#!/usr/bin/env python3
"""Probe an OpenAI-compatible API without third-party dependencies."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import http.client
import json
import math
import os
import re
import socket
import ssl
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, BinaryIO, Iterable, Mapping


VERSION = "1.0.0"
SCHEMA_VERSION = "1.0"
DEFAULT_CHECKS = ("models", "chat", "stream")
DEFAULT_PROMPT = "Reply with exactly OK."
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_ERROR_BYTES = 32 * 1024
MAX_SSE_BYTES = 4 * 1024 * 1024
MAX_SSE_LINE_BYTES = 256 * 1024
MAX_SSE_EVENTS = 10_000
MAX_INCLUDED_CONTENT_CHARS = 4_096
USER_AGENT = f"ai-api-relay-checker/{VERSION}"


class ProbeFailure(Exception):
    def __init__(self, kind: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.details = details

    def add_details(self, **details: Any) -> "ProbeFailure":
        self.details = {**details, **self.details}
        return self


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Keep bearer credentials on the original endpoint only."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


class SafeArgumentParser(argparse.ArgumentParser):
    def parse_args(self, args=None, namespace=None):  # noqa: ANN001
        parsed, unknown = self.parse_known_args(args, namespace)
        if unknown:
            safe_unknown = []
            for item in unknown:
                if item.startswith("-"):
                    option, separator, _value = item.partition("=")
                    safe_unknown.append(option + "=[REDACTED]" if separator else option)
                else:
                    safe_unknown.append("[REDACTED]")
            self.error("unrecognized arguments: " + " ".join(safe_unknown))
        return parsed

    def error(self, message: str) -> None:
        super().error(redact_text(message, ()))


def redact_text(value: str, secrets: Iterable[str]) -> str:
    redacted = value
    for secret in sorted({item for item in secrets if item}, key=len, reverse=True):
        redacted = redacted.replace(secret, "[REDACTED]")
    redacted = re.sub(
        r"(?i)\bBearer\s+[^\s\"'\\]+",
        "Bearer [REDACTED]",
        redacted,
    )
    redacted = re.sub(
        r"(?i)(\b(?:api[-_ ]?key|access[-_ ]?token|authorization|secret)"
        r"[\"']?\s*[:=]\s*[\"']?)([^\s\"',}]+)",
        r"\1[REDACTED]",
        redacted,
    )
    redacted = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[REDACTED]", redacted)
    return redacted


def redact_tree(value: Any, secrets: Iterable[str]) -> Any:
    if isinstance(value, str):
        return redact_text(value, secrets)
    if isinstance(value, list):
        return [redact_tree(item, secrets) for item in value]
    if isinstance(value, tuple):
        return [redact_tree(item, secrets) for item in value]
    if isinstance(value, dict):
        return {str(key): redact_tree(item, secrets) for key, item in value.items()}
    return value


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)


def build_parser() -> argparse.ArgumentParser:
    parser = SafeArgumentParser(
        prog="ai_api_relay_checker.py",
        allow_abbrev=False,
        description=(
            "Probe /models, non-streaming chat completions, and SSE streaming on an "
            "OpenAI-compatible API. The API key is read only from an environment variable."
        ),
    )
    parser.add_argument("--base-url", required=True, help="API prefix, usually https://host/v1")
    parser.add_argument("--model", help="model ID used by chat probes")
    parser.add_argument(
        "--api-key-env",
        default="OPENAI_API_KEY",
        help="environment variable containing the API key (default: OPENAI_API_KEY)",
    )
    parser.add_argument(
        "--check",
        action="append",
        choices=DEFAULT_CHECKS,
        help="check to run; repeat to select multiple checks (default: all)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="per-operation timeout in seconds, from 0.1 to 120 (default: 15)",
    )
    parser.add_argument(
        "--prompt",
        default=DEFAULT_PROMPT,
        help="minimal chat probe prompt; omitted from reports",
    )
    parser.add_argument(
        "--include-content",
        action="store_true",
        help="include up to 4096 response characters; disabled by default",
    )
    parser.add_argument(
        "--output",
        default="-",
        help="JSON report path, or - for stdout (default: -)",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return parser


def validate_base_url(raw_url: str, parser: argparse.ArgumentParser) -> str:
    if not raw_url or len(raw_url) > 2_048:
        parser.error("base URL must contain between 1 and 2048 characters")
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in raw_url):
        parser.error("base URL must not contain whitespace or control characters")
    if not raw_url.isascii():
        parser.error("base URL must use ASCII; encode IDN hosts and URL paths first")
    try:
        parts = urllib.parse.urlsplit(raw_url)
    except ValueError as exc:
        parser.error(f"invalid base URL: {exc}")
    if parts.scheme.lower() not in {"http", "https"}:
        parser.error("base URL must use http:// or https://")
    if not parts.netloc or not parts.hostname:
        parser.error("base URL must include a hostname")
    if parts.username is not None or parts.password is not None:
        parser.error("base URL must not include credentials")
    if parts.query or parts.fragment:
        parser.error("base URL must not include a query string or fragment")
    try:
        _ = parts.port
    except ValueError as exc:
        parser.error(f"invalid base URL port: {exc}")
    path = parts.path.rstrip("/")
    lower_path = path.lower()
    if lower_path.endswith("/models") or lower_path.endswith("/chat/completions"):
        parser.error("base URL must be an API prefix, not an endpoint path")
    normalized = urllib.parse.urlunsplit(
        (parts.scheme.lower(), parts.netloc, path, "", "")
    )
    return normalized.rstrip("/")


def validate_args(
    args: argparse.Namespace, parser: argparse.ArgumentParser
) -> tuple[str, tuple[str, ...], str]:
    base_url = validate_base_url(args.base_url, parser)
    checks = tuple(args.check or DEFAULT_CHECKS)
    if len(set(checks)) != len(checks):
        parser.error("duplicate check values are not allowed")
    if any(check in {"chat", "stream"} for check in checks):
        if not args.model:
            parser.error("--model is required when chat or stream is selected")
    if args.model is not None:
        if not args.model.strip() or args.model != args.model.strip():
            parser.error("model ID must be non-empty and have no surrounding whitespace")
        if len(args.model) > 256 or any(ord(char) < 32 for char in args.model):
            parser.error("model ID must be at most 256 characters without control characters")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", args.api_key_env or ""):
        parser.error("API key environment variable name is invalid")
    if not math.isfinite(args.timeout) or not 0.1 <= args.timeout <= 120.0:
        parser.error("timeout must be between 0.1 and 120 seconds")
    if not args.prompt.strip():
        parser.error("prompt must not be empty")
    if len(args.prompt) > 4_096:
        parser.error("prompt must be at most 4096 characters")
    if any(char == "\x00" for char in args.prompt):
        parser.error("prompt must not contain NUL characters")
    if not args.output or "\x00" in args.output:
        parser.error("output path must not be empty or contain NUL characters")

    api_key = os.environ.get(args.api_key_env)
    if api_key is None or not api_key:
        parser.error(f"environment variable {args.api_key_env} is not set or empty")
    if len(api_key) > 4_096:
        parser.error(f"environment variable {args.api_key_env} exceeds 4096 characters")
    if any(not 33 <= ord(char) <= 126 for char in api_key):
        parser.error(
            f"environment variable {args.api_key_env} must contain printable ASCII without whitespace"
        )
    return base_url, checks, api_key


def endpoint(base_url: str, suffix: str) -> str:
    return f"{base_url}/{suffix.lstrip('/')}"


def request_headers(api_key: str, accept: str) -> dict[str, str]:
    return {
        "Accept": accept,
        "Authorization": f"Bearer {api_key}",
        "User-Agent": USER_AGENT,
    }


def response_metadata(
    response: Any,
    started: float,
    secrets: Iterable[str] = (),
) -> dict[str, Any]:
    content_type = redact_text(response.headers.get("Content-Type", ""), secrets)
    metadata: dict[str, Any] = {
        "status_code": int(response.status),
        "latency_ms": elapsed_ms(started),
        "content_type": content_type,
    }
    for header_name in ("X-Request-ID", "Request-ID", "CF-Ray"):
        value = response.headers.get(header_name)
        if value:
            metadata["request_id"] = redacted_excerpt(value, secrets, 512)
            break
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        metadata["retry_after"] = redacted_excerpt(retry_after, secrets, 128)
    return metadata


def media_type(content_type: str) -> str:
    return content_type.split(";", 1)[0].strip().lower()


def is_json_content_type(content_type: str) -> bool:
    value = media_type(content_type)
    return value == "application/json" or value.endswith("+json")


def read_limited(
    stream: BinaryIO,
    limit: int,
    started: float,
    timeout: float,
) -> bytes:
    chunks: list[bytes] = []
    total = 0
    read_method = getattr(stream, "read1", stream.read)
    while True:
        if time.perf_counter() - started > timeout:
            raise ProbeFailure("timeout", "response exceeded the configured deadline")
        chunk = read_method(min(65_536, limit + 1 - total))
        if time.perf_counter() - started > timeout:
            raise ProbeFailure("timeout", "response exceeded the configured deadline")
        if not chunk:
            remaining = getattr(stream, "length", None)
            if isinstance(remaining, int) and remaining > 0:
                raise ProbeFailure(
                    "network_error",
                    "response ended before the declared Content-Length",
                )
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
        if total > limit:
            raise ProbeFailure(
                "response_too_large",
                f"response exceeded the {limit}-byte safety limit",
            )


def redacted_excerpt(value: str, secrets: Iterable[str], limit: int = 300) -> str:
    return redact_text(value, secrets)[:limit]


def decode_error_message(body: bytes, secrets: Iterable[str]) -> str:
    if not body:
        return "empty response body"
    text = body.decode("utf-8", errors="replace").strip()
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        compact = " ".join(text.split())
        return redacted_excerpt(compact, secrets) if compact else "unreadable response body"
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return redacted_excerpt(error["message"], secrets)
        if isinstance(error, str):
            return redacted_excerpt(error, secrets)
        if isinstance(payload.get("message"), str):
            return redacted_excerpt(payload["message"], secrets)
    compact = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return redacted_excerpt(compact, secrets)


def classify_http_status(status_code: int) -> str:
    if status_code in {401, 403}:
        return "authentication_error"
    if status_code == 404:
        return "not_found"
    if status_code == 429:
        return "rate_limited"
    if 500 <= status_code <= 599:
        return "server_error"
    if 300 <= status_code <= 399:
        return "redirect_rejected"
    return "http_error"


def network_failure(exc: BaseException, started: float) -> ProbeFailure:
    reason: Any = exc
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
    details = {"latency_ms": elapsed_ms(started)}
    if isinstance(reason, (socket.timeout, TimeoutError)):
        return ProbeFailure("timeout", "request timed out", **details)
    if isinstance(reason, ssl.SSLCertVerificationError):
        return ProbeFailure("tls_error", "TLS certificate verification failed", **details)
    if isinstance(reason, ssl.SSLError):
        return ProbeFailure("tls_error", f"TLS handshake failed: {reason}", **details)
    if isinstance(reason, socket.gaierror):
        return ProbeFailure("dns_error", f"DNS resolution failed: {reason}", **details)
    return ProbeFailure("network_error", f"network request failed: {reason}", **details)


def open_request(
    url: str,
    api_key: str,
    timeout: float,
    *,
    payload: Mapping[str, Any] | None = None,
    accept: str,
    sensitive_values: Iterable[str] = (),
) -> tuple[Any, float]:
    body: bytes | None = None
    all_sensitive = (api_key, *tuple(sensitive_values))
    headers = request_headers(api_key, accept)
    method = "GET"
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
        method = "POST"
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    opener = urllib.request.build_opener(NoRedirectHandler())
    started = time.perf_counter()
    try:
        response = opener.open(request, timeout=timeout)
        return response, started
    except urllib.error.HTTPError as exc:
        metadata = response_metadata(exc, started, all_sensitive)
        try:
            try:
                body_bytes = read_limited(exc, MAX_ERROR_BYTES, started, timeout)
            except ProbeFailure as body_failure:
                body_bytes = b""
                metadata["error_body"] = body_failure.kind
            except (
                socket.timeout,
                TimeoutError,
                ssl.SSLError,
                http.client.HTTPException,
                OSError,
            ) as body_exc:
                body_bytes = b""
                metadata["error_body"] = network_failure(body_exc, started).kind
        finally:
            exc.close()
        status_code = int(exc.code)
        metadata["latency_ms"] = elapsed_ms(started)
        message = decode_error_message(body_bytes, all_sensitive)
        raise ProbeFailure(
            classify_http_status(status_code),
            f"HTTP {status_code}: {message}",
            **metadata,
        ) from None
    except (
        urllib.error.URLError,
        socket.timeout,
        TimeoutError,
        ssl.SSLError,
        http.client.HTTPException,
        OSError,
    ) as exc:
        raise network_failure(exc, started) from None


def request_json(
    url: str,
    api_key: str,
    timeout: float,
    payload: Mapping[str, Any] | None = None,
    sensitive_values: Iterable[str] = (),
) -> tuple[Any, dict[str, Any]]:
    response, started = open_request(
        url,
        api_key,
        timeout,
        payload=payload,
        accept="application/json",
        sensitive_values=sensitive_values,
    )
    try:
        metadata = response_metadata(
            response,
            started,
            (api_key, *tuple(sensitive_values)),
        )
        if not is_json_content_type(metadata["content_type"]):
            raise ProbeFailure(
                "unexpected_content_type",
                f"expected JSON but received {metadata['content_type'] or 'no Content-Type'}",
                **metadata,
            )
        try:
            body = read_limited(response, MAX_JSON_BYTES, started, timeout)
        except ProbeFailure as exc:
            metadata["latency_ms"] = elapsed_ms(started)
            raise exc.add_details(**metadata)
        except (
            socket.timeout,
            TimeoutError,
            ssl.SSLError,
            http.client.HTTPException,
            OSError,
        ) as exc:
            failure = network_failure(exc, started)
            raise failure.add_details(**metadata)
    finally:
        response.close()
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ProbeFailure(
            "invalid_json_encoding",
            f"response is not valid UTF-8 at byte {exc.start}",
            **metadata,
        ) from None
    metadata["latency_ms"] = elapsed_ms(started)
    try:
        return strict_json_loads(text), metadata
    except (InvalidJSONConstant, InvalidUnicodeScalar) as exc:
        raise ProbeFailure(
            "invalid_json",
            f"invalid JSON value: {exc}",
            **metadata,
        ) from None
    except json.JSONDecodeError as exc:
        raise ProbeFailure(
            "invalid_json",
            f"invalid JSON at line {exc.lineno}, column {exc.colno}",
            **metadata,
        ) from None


class InvalidJSONConstant(ValueError):
    pass


class InvalidUnicodeScalar(ValueError):
    pass


def reject_json_constant(value: str) -> None:
    raise InvalidJSONConstant(value)


def strict_json_loads(value: str) -> Any:
    parsed = json.loads(value, parse_constant=reject_json_constant)
    validate_unicode_scalars(parsed)
    return parsed


def validate_unicode_scalars(value: Any) -> None:
    if isinstance(value, str):
        if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
            raise InvalidUnicodeScalar("unpaired UTF-16 surrogate")
        return
    if isinstance(value, list):
        for item in value:
            validate_unicode_scalars(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            validate_unicode_scalars(key)
            validate_unicode_scalars(item)


def api_error_message(
    payload: Mapping[str, Any],
    secrets: Iterable[str],
) -> str | None:
    error = payload.get("error")
    if error is None:
        return None
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        if not error["message"]:
            return "response contained an empty API error message"
        return redacted_excerpt(error["message"], secrets)
    if isinstance(error, str):
        if not error:
            return "response contained an empty API error string"
        return redacted_excerpt(error, secrets)
    return "response contained an API error object"


def normalize_usage(value: Any) -> dict[str, int | float]:
    if not isinstance(value, dict):
        return {}
    safe_usage: dict[str, int | float] = {}
    for key in (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "input_tokens",
        "output_tokens",
    ):
        number = value.get(key)
        if isinstance(number, (int, float)) and not isinstance(number, bool) and math.isfinite(number):
            safe_usage[key] = number
    return safe_usage


def extract_text_content(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for part in value:
            if not isinstance(part, dict) or not isinstance(part.get("text"), str):
                raise ProbeFailure(
                    "invalid_schema",
                    "assistant content parts must contain string text fields",
                )
            parts.append(part["text"])
        return "".join(parts)
    raise ProbeFailure("invalid_schema", "assistant content must be a string, list, or null")


def maybe_include_content(
    result: dict[str, Any],
    content: str,
    enabled: bool,
    secrets: Iterable[str],
) -> None:
    safe_content = redact_text(content, secrets)
    result["content_chars"] = len(safe_content)
    result["content_sha256"] = hashlib.sha256(safe_content.encode("utf-8")).hexdigest()
    if enabled:
        result["content"] = safe_content[:MAX_INCLUDED_CONTENT_CHARS]
        result["content_truncated"] = len(safe_content) > MAX_INCLUDED_CONTENT_CHARS


def probe_models(
    url: str,
    api_key: str,
    timeout: float,
    model: str | None,
    _prompt: str,
    _include_content: bool,
) -> dict[str, Any]:
    payload, metadata = request_json(url, api_key, timeout)
    if not isinstance(payload, dict):
        raise ProbeFailure(
            "invalid_schema",
            "models response must be a JSON object",
            **metadata,
        )
    error_message = api_error_message(payload, (api_key,))
    if error_message is not None:
        raise ProbeFailure("api_error", error_message, **metadata)
    if not isinstance(payload.get("data"), list):
        raise ProbeFailure(
            "invalid_schema",
            "models response must contain a data array",
            **metadata,
        )
    model_ids: list[str] = []
    for item in payload["data"]:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
            raise ProbeFailure(
                "invalid_schema",
                "every models data item must contain a non-empty string id",
                **metadata,
            )
        model_ids.append(item["id"])
    safe_model_ids = [redact_text(item, (api_key,)) for item in model_ids]
    result: dict[str, Any] = {
        **metadata,
        "model_count": len(model_ids),
        "model_ids_sample": safe_model_ids[:25],
        "model_ids_truncated": len(model_ids) > 25,
    }
    if model is not None:
        present = model in model_ids
        result["target_model_present"] = present
        if not present:
            raise ProbeFailure(
                "model_not_listed",
                "the requested model was not present in /models",
                **result,
            )
    return result


def chat_payload(model: str, prompt: str, stream: bool) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": stream,
    }


def probe_chat(
    url: str,
    api_key: str,
    timeout: float,
    model: str | None,
    prompt: str,
    include_content: bool,
) -> dict[str, Any]:
    assert model is not None
    payload, metadata = request_json(
        url,
        api_key,
        timeout,
        chat_payload(model, prompt, False),
        sensitive_values=(prompt,),
    )
    if not isinstance(payload, dict):
        raise ProbeFailure("invalid_schema", "chat response must be a JSON object", **metadata)
    error_message = api_error_message(payload, (api_key, prompt))
    if error_message is not None:
        raise ProbeFailure("api_error", error_message, **metadata)
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ProbeFailure(
            "invalid_schema",
            "chat response must contain at least one choice",
            **metadata,
        )
    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, dict) or "content" not in message:
        raise ProbeFailure(
            "invalid_schema",
            "first chat choice must contain message.content",
            **metadata,
        )
    try:
        content = extract_text_content(message["content"])
    except ProbeFailure as exc:
        raise exc.add_details(**metadata)
    result: dict[str, Any] = dict(metadata)
    response_id = payload.get("id")
    if isinstance(response_id, str):
        result["response_id"] = redacted_excerpt(response_id, (api_key, prompt), 512)
    finish_reason = choice.get("finish_reason")
    if isinstance(finish_reason, str):
        result["finish_reason"] = redacted_excerpt(
            finish_reason,
            (api_key, prompt),
            128,
        )
    usage = normalize_usage(payload.get("usage"))
    if usage:
        result["usage"] = usage
    maybe_include_content(result, content, include_content, (api_key, prompt))
    return result


def sse_fields(lines: list[str]) -> str | None:
    data_lines: list[str] = []
    for line in lines:
        if not line or line.startswith(":"):
            continue
        if ":" in line:
            field, value = line.split(":", 1)
            if value.startswith(" "):
                value = value[1:]
        else:
            field, value = line, ""
        if field == "data":
            data_lines.append(value)
    return "\n".join(data_lines) if data_lines else None


def iter_sse_lines(
    response: BinaryIO,
    started: float,
    timeout: float,
) -> Iterable[str]:
    total_bytes = 0
    line = bytearray()
    skip_lf = False
    first_line = True
    read_method = getattr(response, "read1", response.read)
    while True:
        if time.perf_counter() - started > timeout:
            raise ProbeFailure("timeout", "SSE probe exceeded the configured deadline")
        chunk = read_method(8_192)
        if time.perf_counter() - started > timeout:
            raise ProbeFailure("timeout", "SSE probe exceeded the configured deadline")
        if not chunk:
            if line:
                try:
                    decoded = bytes(line).decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise ProbeFailure(
                        "invalid_sse_encoding",
                        f"SSE stream is not valid UTF-8 at byte {exc.start}",
                    ) from None
                if first_line and decoded.startswith("\ufeff"):
                    decoded = decoded[1:]
                yield decoded
            return
        for byte in chunk:
            total_bytes += 1
            if total_bytes > MAX_SSE_BYTES:
                raise ProbeFailure("response_too_large", "SSE response exceeded the safety limit")
            if skip_lf:
                skip_lf = False
                if byte == 0x0A:
                    continue
            if byte in {0x0A, 0x0D}:
                try:
                    decoded = bytes(line).decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise ProbeFailure(
                        "invalid_sse_encoding",
                        f"SSE stream is not valid UTF-8 at byte {exc.start}",
                    ) from None
                line.clear()
                if first_line and decoded.startswith("\ufeff"):
                    decoded = decoded[1:]
                first_line = False
                yield decoded
                skip_lf = byte == 0x0D
            else:
                line.append(byte)
                if len(line) > MAX_SSE_LINE_BYTES:
                    raise ProbeFailure("sse_line_too_large", "SSE line exceeded the safety limit")


def iter_sse_data(
    response: BinaryIO,
    started: float,
    timeout: float,
) -> Iterable[str]:
    event_lines: list[str] = []
    for line in iter_sse_lines(response, started, timeout):
        if line == "":
            data = sse_fields(event_lines)
            event_lines = []
            if data is not None:
                yield data
        else:
            event_lines.append(line)
    data = sse_fields(event_lines)
    if data is not None:
        yield data


def parse_sse_response(
    response: Any,
    started: float,
    metadata: dict[str, Any],
    include_content: bool,
    secrets: Iterable[str],
    timeout: float,
) -> dict[str, Any]:
    event_count = 0
    json_event_count = 0
    choice_event_count = 0
    delta_event_count = 0
    completed = False
    first_event_ms: float | None = None
    content_parts: list[str] = []
    finish_reason: str | None = None
    try:
        for data in iter_sse_data(response, started, timeout):
            event_count += 1
            if event_count > MAX_SSE_EVENTS:
                raise ProbeFailure("too_many_sse_events", "SSE event count exceeded the safety limit")
            if first_event_ms is None:
                first_event_ms = elapsed_ms(started)
            if data == "[DONE]":
                completed = True
                break
            try:
                event = strict_json_loads(data)
            except (InvalidJSONConstant, InvalidUnicodeScalar) as exc:
                raise ProbeFailure(
                    "invalid_sse_json",
                    f"invalid SSE JSON value: {exc}",
                ) from None
            except json.JSONDecodeError as exc:
                raise ProbeFailure(
                    "invalid_sse_json",
                    f"invalid SSE JSON at line {exc.lineno}, column {exc.colno}",
                ) from None
            if not isinstance(event, dict):
                raise ProbeFailure("invalid_sse_schema", "SSE data must decode to a JSON object")
            json_event_count += 1
            error_message = api_error_message(event, secrets)
            if error_message is not None:
                raise ProbeFailure("api_error", error_message)
            choices = event.get("choices")
            if not isinstance(choices, list):
                raise ProbeFailure("invalid_sse_schema", "SSE event must contain a choices array")
            if choices:
                choice_event_count += 1
            for choice in choices:
                if not isinstance(choice, dict):
                    raise ProbeFailure("invalid_sse_schema", "SSE choice must be an object")
                delta = choice.get("delta")
                if delta is not None:
                    if not isinstance(delta, dict):
                        raise ProbeFailure("invalid_sse_schema", "SSE choice delta must be an object")
                    if "content" in delta:
                        try:
                            text = extract_text_content(delta["content"])
                        except ProbeFailure as exc:
                            exc.kind = "invalid_sse_schema"
                            raise
                        if text:
                            content_parts.append(text)
                            delta_event_count += 1
                candidate = choice.get("finish_reason")
                if isinstance(candidate, str):
                    finish_reason = redacted_excerpt(candidate, secrets, 128)
    except ProbeFailure as exc:
        details = {
            **metadata,
            "latency_ms": elapsed_ms(started),
            "event_count": event_count,
            "completed": completed,
        }
        raise exc.add_details(**details)
    except (
        socket.timeout,
        TimeoutError,
        ssl.SSLError,
        http.client.HTTPException,
        OSError,
    ) as exc:
        failure = network_failure(exc, started)
        raise failure.add_details(**metadata, event_count=event_count, completed=completed)
    if not completed:
        details = {
            **metadata,
            "latency_ms": elapsed_ms(started),
            "event_count": event_count,
            "completed": False,
        }
        raise ProbeFailure(
            "sse_incomplete",
            "SSE stream ended before the [DONE] marker",
            **details,
        )
    if json_event_count == 0:
        details = {
            **metadata,
            "latency_ms": elapsed_ms(started),
            "event_count": event_count,
            "completed": True,
        }
        raise ProbeFailure(
            "invalid_sse_schema",
            "SSE stream completed without a JSON data event",
            **details,
        )
    if choice_event_count == 0:
        details = {
            **metadata,
            "latency_ms": elapsed_ms(started),
            "event_count": event_count,
            "completed": True,
        }
        raise ProbeFailure(
            "invalid_sse_schema",
            "SSE stream completed without a non-empty choices event",
            **details,
        )
    content = "".join(content_parts)
    result: dict[str, Any] = {
        **metadata,
        "latency_ms": elapsed_ms(started),
        "first_event_ms": first_event_ms,
        "event_count": event_count,
        "json_event_count": json_event_count,
        "choice_event_count": choice_event_count,
        "delta_event_count": delta_event_count,
        "completed": True,
    }
    if finish_reason:
        result["finish_reason"] = finish_reason
    maybe_include_content(result, content, include_content, secrets)
    return result


def probe_stream(
    url: str,
    api_key: str,
    timeout: float,
    model: str | None,
    prompt: str,
    include_content: bool,
) -> dict[str, Any]:
    assert model is not None
    response, started = open_request(
        url,
        api_key,
        timeout,
        payload=chat_payload(model, prompt, True),
        accept="text/event-stream",
        sensitive_values=(prompt,),
    )
    try:
        metadata = response_metadata(response, started, (api_key, prompt))
        if media_type(metadata["content_type"]) != "text/event-stream":
            raise ProbeFailure(
                "unexpected_content_type",
                f"expected text/event-stream but received {metadata['content_type'] or 'no Content-Type'}",
                **metadata,
            )
        return parse_sse_response(
            response,
            started,
            metadata,
            include_content,
            (api_key, prompt),
            timeout,
        )
    finally:
        response.close()


def failure_result(
    name: str,
    url: str,
    failure: ProbeFailure,
    prompt: str,
) -> dict[str, Any]:
    return {
        "name": name,
        "endpoint": url,
        "ok": False,
        **failure.details,
        "error": {
            "kind": failure.kind,
            "message": redact_text(failure.message, (prompt,)),
        },
    }


def run_check(
    name: str,
    url: str,
    function: Any,
    api_key: str,
    timeout: float,
    model: str | None,
    prompt: str,
    include_content: bool,
) -> dict[str, Any]:
    try:
        details = function(url, api_key, timeout, model, prompt, include_content)
        return {"name": name, "endpoint": url, "ok": True, **details}
    except ProbeFailure as exc:
        return failure_result(name, url, exc, prompt)
    except Exception as exc:  # Last-resort report stability; never expose a traceback or object repr.
        failure = ProbeFailure(
            "internal_error",
            f"unexpected internal error ({type(exc).__name__})",
        )
        return failure_result(name, url, failure, prompt)


def build_report(
    base_url: str,
    checks: tuple[str, ...],
    api_key_env: str,
    api_key: str,
    timeout: float,
    model: str | None,
    prompt: str,
    include_content: bool,
) -> dict[str, Any]:
    registry = {
        "models": (probe_models, endpoint(base_url, "models")),
        "chat": (probe_chat, endpoint(base_url, "chat/completions")),
        "stream": (probe_stream, endpoint(base_url, "chat/completions")),
    }
    results: list[dict[str, Any]] = []
    for name in checks:
        function, url = registry[name]
        results.append(
            run_check(
                name,
                url,
                function,
                api_key,
                timeout,
                model,
                prompt,
                include_content,
            )
        )
    passed = sum(1 for item in results if item["ok"])
    failed = len(results) - passed
    status = "ok" if failed == 0 else "failed" if passed == 0 else "degraded"
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "tool": {"name": "ai-api-relay-checker", "version": VERSION},
        "generated_at": utc_now(),
        "configuration": {
            "base_url": base_url,
            "checks": list(checks),
            "api_key_env": api_key_env,
            "timeout_seconds": timeout,
            "model": model,
            "prompt_chars": len(prompt),
            "include_content": include_content,
        },
        "summary": {
            "checks_total": len(results),
            "failed": failed,
            "passed": passed,
            "status": status,
        },
        "checks": results,
    }
    return redact_tree(report, (api_key,))


def render_report(report: Mapping[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True) + "\n"


def write_report(destination: str, rendered: str) -> None:
    if destination == "-":
        sys.stdout.write(rendered)
        sys.stdout.flush()
        return
    target = Path(destination).expanduser()
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(parent),
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(rendered)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, target)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    base_url, checks, api_key = validate_args(args, parser)
    report = build_report(
        base_url=base_url,
        checks=checks,
        api_key_env=args.api_key_env,
        api_key=api_key,
        timeout=args.timeout,
        model=args.model,
        prompt=args.prompt,
        include_content=args.include_content,
    )
    rendered = render_report(report)
    try:
        write_report(args.output, rendered)
    except OSError as exc:
        parser.error(f"unable to write JSON report: {exc}")
    return 0 if report["summary"]["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

