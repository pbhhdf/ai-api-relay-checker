import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CHECKER = REPO_ROOT / "ai_api_relay_checker.py"


class RelayMockHandler(BaseHTTPRequestHandler):
    api_key = "sk-local-test-secret-1234567890"
    requests_seen = []

    def log_message(self, _format, *_args):
        return

    def _mode(self):
        parts = [part for part in self.path.split("?", 1)[0].split("/") if part]
        return parts[0] if parts else "ok"

    def _record(self, payload=None):
        self.__class__.requests_seen.append(
            {
                "method": self.command,
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "payload": payload,
            }
        )

    def _send_bytes(self, status, body, content_type, headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Request-ID", "req-local-123")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        try:
            self.wfile.write(body)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _send_json(self, status, payload, headers=None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8", headers)

    def _authorize_or_reject(self):
        expected = f"Bearer {self.api_key}"
        if self.headers.get("Authorization") == expected:
            return True
        self._send_json(401, {"error": {"message": "missing or invalid bearer token"}})
        return False

    def _forced_http_error(self, mode):
        if mode == "redirect":
            self.send_response(302)
            self.send_header("Location", "/ok/v1/models")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return True
        if mode == "auth-error":
            leaked = self.headers.get("Authorization", "missing")
            self._send_json(401, {"error": {"message": f"rejected {leaked}"}})
            return True
        if mode == "bare-key-error":
            leaked = self.headers.get("Authorization", "missing")
            if leaked.startswith("Bearer "):
                leaked = leaked[len("Bearer ") :]
            self._send_json(401, {"error": {"message": leaked}})
            return True
        if mode == "header-key-error":
            body = b'{"error":{"message":"rejected"}}'
            leaked = self.headers.get("Authorization", "missing")
            if leaked.startswith("Bearer "):
                leaked = leaked[len("Bearer ") :]
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Request-ID", f"prefix-{leaked}")
            self.end_headers()
            self.wfile.write(body)
            return True
        if mode == "rate-limit":
            self._send_json(
                429,
                {"error": {"message": "quota exhausted"}},
                {"Retry-After": "3"},
            )
            return True
        if mode == "server-error":
            self._send_json(503, {"error": {"message": "temporary upstream failure"}})
            return True
        return False

    def do_GET(self):
        mode = self._mode()
        self._record()
        if self._forced_http_error(mode):
            return
        if not self._authorize_or_reject():
            return
        if mode == "slow":
            time.sleep(0.35)
        if mode == "bad-json":
            self._send_bytes(200, b"{not-json", "application/json")
        elif mode == "nan-json":
            self._send_bytes(200, b'{"data":[],"value":NaN}', "application/json")
        elif mode == "surrogate-json":
            self._send_bytes(200, b'{"data":[{"id":"\\ud800"}]}', "application/json")
        elif mode == "unicode-model":
            self._send_json(200, {"data": [{"id": "模型-😀"}]})
        elif mode == "truncated-json-body":
            body = b'{"data":[]}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body) + 100))
            self.end_headers()
            self.wfile.write(body)
        elif mode == "wrong-type":
            self._send_bytes(200, b"<html>not json</html>", "text/html")
        elif mode == "bad-model-shape":
            self._send_json(200, {"object": "list", "data": {}})
        elif mode == "models-api-error":
            self._send_json(200, {"error": {"message": "logical API failure"}})
        elif mode == "empty-api-error":
            self._send_json(200, {"error": {"message": ""}})
        elif mode == "models-missing":
            self._send_json(200, {"object": "list", "data": [{"id": "other-model"}]})
        else:
            self._send_json(
                200,
                {
                    "object": "list",
                    "data": [{"id": "gpt-test"}, {"id": "other-model"}],
                },
            )

    def do_POST(self):
        mode = self._mode()
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError:
            length = 0
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = None
        self._record(payload)
        if self._forced_http_error(mode):
            return
        if not self._authorize_or_reject():
            return
        if mode == "slow":
            time.sleep(0.35)
        if not isinstance(payload, dict):
            self._send_json(400, {"error": {"message": "invalid request JSON"}})
            return
        if mode == "prompt-error":
            prompt = payload.get("messages", [{}])[0].get("content", "")
            self._send_json(400, {"error": {"message": prompt}})
            return
        if mode == "chat-prompt-api-error":
            prompt = payload.get("messages", [{}])[0].get("content", "")
            self._send_json(200, {"error": {"message": prompt}})
            return
        if payload.get("stream"):
            self._stream_response(mode)
        else:
            self._chat_response(mode, payload)

    def _chat_response(self, mode, payload):
        if mode == "bad-chat-json":
            self._send_bytes(200, b"[broken", "application/json")
        elif mode == "bad-chat-shape":
            self._send_json(200, {"id": "chatcmpl-local", "choices": []})
        elif mode == "wrong-type":
            self._send_bytes(200, b"not json", "text/plain")
        else:
            content = self.api_key if mode == "echo-key" else "assistant reply marker"
            response_id = (
                payload.get("messages", [{}])[0].get("content", "")
                if mode == "chat-metadata-prompt"
                else "chatcmpl-local"
            )
            finish_reason = response_id if mode == "chat-metadata-prompt" else "stop"
            self._send_json(
                200,
                {
                    "id": response_id,
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": content,
                            },
                            "finish_reason": finish_reason,
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 7,
                        "completion_tokens": 3,
                        "total_tokens": 10,
                    },
                },
            )

    def _stream_response(self, mode):
        if mode == "json-stream":
            self._send_json(200, {"id": "not-an-event-stream"})
            return
        if mode == "wrong-type":
            self._send_bytes(200, b"not an event stream", "text/plain")
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Request-ID", "req-stream-local")
        self.end_headers()

        if mode == "bad-sse-json":
            frames = [b"data: {broken-json\n\n", b"data: [DONE]\n\n"]
        elif mode == "nan-sse":
            frames = [b'data: {"choices":[],"value":NaN}\n\n', b"data: [DONE]\n\n"]
        elif mode == "empty-sse":
            frames = [b'data: {"choices":[]}\n\n', b"data: [DONE]\n\n"]
        elif mode == "bom-cr-sse":
            frames = [
                b'\xef\xbb\xbfdata: {"choices":[{"delta":{"content":"OK"},"finish_reason":"stop"}]}\r\rdata: [DONE]\r\r'
            ]
        elif mode == "sse-api-error":
            frames = [
                b'data: {"error":{"message":"upstream stream failed"}}\n\n',
                b"data: [DONE]\n\n",
            ]
        elif mode == "invalid-utf8-sse":
            frames = [b"data: \xff\n\n"]
        elif mode == "truncated-sse":
            frames = [
                b'data: {"id":"chatcmpl-stream","choices":[{"delta":{"content":"partial"},"finish_reason":null}]}\n\n'
            ]
        else:
            frames = [
                b": keep-alive\r\n\r\n",
                b'data: {"id":"chatcmpl-stream","choices":[{"delta":{"content":"stream "},"finish_reason":null}]}\r\n\r\n',
                b'data: {"id":"chatcmpl-stream","choices":[{"delta":{"content":"reply marker"},"finish_reason":null}]}\r\n\r\n',
                b'data: {"id":"chatcmpl-stream","choices":[{"delta":{},"finish_reason":"stop"}]}\r\n\r\n',
                b'data: {"id":"chatcmpl-stream","choices":[],"usage":{"total_tokens":10}}\r\n\r\n',
                b"data: [DONE]\r\n\r\n",
            ]
        try:
            for frame in frames:
                self.wfile.write(frame)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


class CheckerCLITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), RelayMockHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.origin = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        RelayMockHandler.requests_seen = []

    def base_url(self, mode="ok"):
        return f"{self.origin}/{mode}/v1"

    def run_cli(
        self,
        *args,
        key=RelayMockHandler.api_key,
        env_name="TEST_API_KEY",
        env_overrides=None,
    ):
        env = os.environ.copy()
        env.pop("OPENAI_API_KEY", None)
        env.pop(env_name, None)
        if key is not None:
            env[env_name] = key
        env.update(env_overrides or {})
        command = [
            sys.executable,
            str(CHECKER),
            "--api-key-env",
            env_name,
            *map(str, args),
        ]
        return subprocess.run(
            command,
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
            check=False,
        )

    def load_stdout_report(self, result):
        self.assertTrue(result.stdout.strip(), result.stderr)
        return json.loads(result.stdout)

    def test_all_probes_succeed_and_hide_response_content_by_default(self):
        result = self.run_cli(
            "--base-url",
            self.base_url(),
            "--model",
            "gpt-test",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.load_stdout_report(result)
        self.assertEqual(report["schema_version"], "1.0")
        self.assertEqual(report["summary"], {"checks_total": 3, "failed": 0, "passed": 3, "status": "ok"})
        self.assertEqual([item["name"] for item in report["checks"]], ["models", "chat", "stream"])
        self.assertTrue(report["checks"][0]["target_model_present"])
        self.assertEqual(report["checks"][1]["content_chars"], 22)
        self.assertTrue(report["checks"][2]["completed"])
        self.assertNotIn("assistant reply marker", result.stdout)
        self.assertNotIn("stream reply marker", result.stdout)
        self.assertNotIn(RelayMockHandler.api_key, result.stdout)
        self.assertEqual(len(RelayMockHandler.requests_seen), 3)
        self.assertTrue(
            all(
                request["authorization"] == f"Bearer {RelayMockHandler.api_key}"
                for request in RelayMockHandler.requests_seen
            )
        )

    def test_include_content_is_explicit_and_still_runs_through_redaction(self):
        result = self.run_cli(
            "--base-url",
            self.base_url(),
            "--model",
            "gpt-test",
            "--check",
            "chat",
            "--check",
            "stream",
            "--include-content",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.load_stdout_report(result)
        self.assertEqual(report["checks"][0]["content"], "assistant reply marker")
        self.assertEqual(report["checks"][1]["content"], "stream reply marker")
        self.assertNotIn(RelayMockHandler.api_key, result.stdout)

    def test_response_content_and_its_hash_use_the_redacted_value(self):
        result = self.run_cli(
            "--base-url",
            self.base_url("echo-key"),
            "--model",
            "gpt-test",
            "--check",
            "chat",
            "--include-content",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        check = self.load_stdout_report(result)["checks"][0]
        self.assertEqual(check["content"], "[REDACTED]")
        self.assertEqual(
            check["content_sha256"],
            hashlib.sha256(b"[REDACTED]").hexdigest(),
        )
        self.assertNotIn(RelayMockHandler.api_key, result.stdout)

    def test_models_only_does_not_require_model(self):
        result = self.run_cli(
            "--base-url",
            self.base_url(),
            "--check",
            "models",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.load_stdout_report(result)
        self.assertEqual(report["checks"][0]["model_count"], 2)
        self.assertNotIn("target_model_present", report["checks"][0])

    def test_partial_failure_produces_degraded_summary_and_nonzero_exit(self):
        result = self.run_cli(
            "--base-url",
            self.base_url("models-missing"),
            "--model",
            "gpt-test",
        )

        self.assertEqual(result.returncode, 1, result.stderr)
        report = self.load_stdout_report(result)
        self.assertEqual(
            report["summary"],
            {"checks_total": 3, "failed": 1, "passed": 2, "status": "degraded"},
        )

    def test_http_errors_are_classified_and_error_bodies_are_redacted(self):
        cases = [
            ("auth-error", 401, "authentication_error"),
            ("rate-limit", 429, "rate_limited"),
            ("server-error", 503, "server_error"),
        ]
        for mode, status_code, error_kind in cases:
            with self.subTest(mode=mode):
                result = self.run_cli(
                    "--base-url",
                    self.base_url(mode),
                    "--check",
                    "models",
                )
                self.assertEqual(result.returncode, 1, result.stderr)
                report = self.load_stdout_report(result)
                check = report["checks"][0]
                self.assertFalse(check["ok"])
                self.assertEqual(check["status_code"], status_code)
                self.assertEqual(check["error"]["kind"], error_kind)
                self.assertNotIn(RelayMockHandler.api_key, result.stdout)
                if mode == "auth-error":
                    self.assertIn("[REDACTED]", result.stdout)
                if mode == "rate-limit":
                    self.assertEqual(check["retry_after"], "3")

    def test_redirect_is_rejected_before_authorization_can_be_forwarded(self):
        result = self.run_cli(
            "--base-url",
            self.base_url("redirect"),
            "--check",
            "models",
        )

        self.assertEqual(result.returncode, 1, result.stderr)
        check = self.load_stdout_report(result)["checks"][0]
        self.assertEqual(check["status_code"], 302)
        self.assertEqual(check["error"]["kind"], "redirect_rejected")
        self.assertEqual(len(RelayMockHandler.requests_seen), 1)

    def test_models_json_content_type_schema_and_target_are_validated(self):
        cases = [
            ("bad-json", "invalid_json"),
            ("nan-json", "invalid_json"),
            ("surrogate-json", "invalid_json"),
            ("truncated-json-body", "network_error"),
            ("wrong-type", "unexpected_content_type"),
            ("bad-model-shape", "invalid_schema"),
            ("models-missing", "model_not_listed"),
            ("models-api-error", "api_error"),
            ("empty-api-error", "api_error"),
        ]
        for mode, error_kind in cases:
            with self.subTest(mode=mode):
                result = self.run_cli(
                    "--base-url",
                    self.base_url(mode),
                    "--model",
                    "gpt-test",
                    "--check",
                    "models",
                )
                self.assertEqual(result.returncode, 1, result.stderr)
                check = self.load_stdout_report(result)["checks"][0]
                self.assertEqual(check["error"]["kind"], error_kind)

    def test_non_stream_chat_json_and_schema_are_validated(self):
        cases = [
            ("bad-chat-json", "invalid_json"),
            ("bad-chat-shape", "invalid_schema"),
            ("wrong-type", "unexpected_content_type"),
        ]
        for mode, error_kind in cases:
            with self.subTest(mode=mode):
                result = self.run_cli(
                    "--base-url",
                    self.base_url(mode),
                    "--model",
                    "gpt-test",
                    "--check",
                    "chat",
                )
                self.assertEqual(result.returncode, 1, result.stderr)
                check = self.load_stdout_report(result)["checks"][0]
                self.assertEqual(check["error"]["kind"], error_kind)

    def test_sse_content_type_json_utf8_api_errors_and_completion_are_validated(self):
        cases = [
            ("truncated-sse", "sse_incomplete"),
            ("bad-sse-json", "invalid_sse_json"),
            ("json-stream", "unexpected_content_type"),
            ("wrong-type", "unexpected_content_type"),
            ("invalid-utf8-sse", "invalid_sse_encoding"),
            ("sse-api-error", "api_error"),
            ("nan-sse", "invalid_sse_json"),
            ("empty-sse", "invalid_sse_schema"),
        ]
        for mode, error_kind in cases:
            with self.subTest(mode=mode):
                result = self.run_cli(
                    "--base-url",
                    self.base_url(mode),
                    "--model",
                    "gpt-test",
                    "--check",
                    "stream",
                )
                self.assertEqual(result.returncode, 1, result.stderr)
                check = self.load_stdout_report(result)["checks"][0]
                self.assertEqual(check["error"]["kind"], error_kind)

    def test_sse_accepts_utf8_bom_and_cr_only_line_endings(self):
        result = self.run_cli(
            "--base-url",
            self.base_url("bom-cr-sse"),
            "--model",
            "gpt-test",
            "--check",
            "stream",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        check = self.load_stdout_report(result)["checks"][0]
        self.assertTrue(check["completed"])
        self.assertEqual(check["choice_event_count"], 1)

    def test_prompt_reflected_by_upstream_is_redacted_from_default_report(self):
        prompt = "P" * 400
        for mode, error_kind in (("prompt-error", "http_error"), ("chat-prompt-api-error", "api_error")):
            with self.subTest(mode=mode):
                result = self.run_cli(
                    "--base-url",
                    self.base_url(mode),
                    "--model",
                    "gpt-test",
                    "--check",
                    "chat",
                    "--prompt",
                    prompt,
                )

                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(
                    self.load_stdout_report(result)["checks"][0]["error"]["kind"],
                    error_kind,
                )
                self.assertNotIn("P" * 50, result.stdout)
                self.assertIn("[REDACTED]", result.stdout)

    def test_long_bare_key_reflected_in_truncated_error_is_fully_redacted(self):
        api_key = "Z" * 400
        for mode in ("bare-key-error", "header-key-error"):
            with self.subTest(mode=mode):
                result = self.run_cli(
                    "--base-url",
                    self.base_url(mode),
                    "--check",
                    "models",
                    key=api_key,
                )

                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertNotIn("Z" * 50, result.stdout)
                self.assertIn("[REDACTED]", result.stdout)

    def test_response_id_and_finish_reason_redact_prompt_before_truncation(self):
        prompt = "R" * 600
        result = self.run_cli(
            "--base-url",
            self.base_url("chat-metadata-prompt"),
            "--model",
            "gpt-test",
            "--check",
            "chat",
            "--prompt",
            prompt,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("R" * 50, result.stdout)
        check = self.load_stdout_report(result)["checks"][0]
        self.assertEqual(check["response_id"], "[REDACTED]")
        self.assertEqual(check["finish_reason"], "[REDACTED]")

    def test_unicode_model_report_survives_ascii_stdout(self):
        result = self.run_cli(
            "--base-url",
            self.base_url("unicode-model"),
            "--check",
            "models",
            env_overrides={"PYTHONIOENCODING": "ascii"},
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.load_stdout_report(result)
        self.assertEqual(report["checks"][0]["model_ids_sample"], ["模型-😀"])
        self.assertNotIn("Traceback", result.stderr)

    def test_timeout_is_reported_without_traceback(self):
        result = self.run_cli(
            "--base-url",
            self.base_url("slow"),
            "--check",
            "models",
            "--timeout",
            "0.1",
        )

        self.assertEqual(result.returncode, 1, result.stderr)
        check = self.load_stdout_report(result)["checks"][0]
        self.assertEqual(check["error"]["kind"], "timeout")
        self.assertNotIn("Traceback", result.stderr + result.stdout)

    def test_missing_key_invalid_key_and_missing_model_are_usage_errors(self):
        missing = self.run_cli(
            "--base-url",
            self.base_url(),
            "--check",
            "models",
            key=None,
        )
        self.assertEqual(missing.returncode, 2)
        self.assertIn("TEST_API_KEY", missing.stderr)
        self.assertNotIn(RelayMockHandler.api_key, missing.stderr)

        invalid = self.run_cli(
            "--base-url",
            self.base_url(),
            "--check",
            "models",
            key="bad\nheader",
        )
        self.assertEqual(invalid.returncode, 2)
        self.assertIn("printable ASCII", invalid.stderr)

        no_model = self.run_cli(
            "--base-url",
            self.base_url(),
            "--check",
            "chat",
        )
        self.assertEqual(no_model.returncode, 2)
        self.assertIn("--model is required", no_model.stderr)

    def test_inline_api_key_option_does_not_exist(self):
        result = self.run_cli(
            "--base-url",
            self.base_url(),
            "--check",
            "models",
            "--api-key",
            RelayMockHandler.api_key,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("unrecognized arguments", result.stderr)
        self.assertNotIn(RelayMockHandler.api_key, result.stderr)

        inline = self.run_cli(
            "--base-url",
            self.base_url(),
            "--check",
            "models",
            f"--api-key={RelayMockHandler.api_key}",
        )
        self.assertEqual(inline.returncode, 2)
        self.assertIn("--api-key=[REDACTED]", inline.stderr)
        self.assertNotIn(RelayMockHandler.api_key, inline.stderr)

    def test_base_url_env_name_timeout_prompt_and_duplicate_checks_are_validated(self):
        invalid_cases = [
            (["--base-url", "ftp://example.test/v1", "--check", "models"], "http:// or https://"),
            (["--base-url", "https://user:pass@example.test/v1", "--check", "models"], "credentials"),
            (["--base-url", "https://example.test/v1?debug=1", "--check", "models"], "query"),
            (["--base-url", "https://example.test/v1/models", "--check", "models"], "API prefix"),
            (["--base-url", "https://exa mple.test/v1", "--check", "models"], "whitespace"),
            (["--base-url", "https://例子.test/v1", "--check", "models"], "ASCII"),
            (["--base-url", self.base_url(), "--check", "models", "--timeout", "0"], "between"),
            (["--base-url", self.base_url(), "--check", "models", "--prompt", "x" * 4097], "4096"),
        ]
        for args, message in invalid_cases:
            with self.subTest(message=message):
                result = self.run_cli(*args)
                self.assertEqual(result.returncode, 2)
                self.assertIn(message, result.stderr)

        bad_env = self.run_cli(
            "--base-url",
            self.base_url(),
            "--check",
            "models",
            env_name="BAD-NAME",
        )
        self.assertEqual(bad_env.returncode, 2)
        self.assertIn("environment variable name", bad_env.stderr)

        duplicate = self.run_cli(
            "--base-url",
            self.base_url(),
            "--check",
            "models",
            "--check",
            "models",
        )
        self.assertEqual(duplicate.returncode, 2)
        self.assertIn("duplicate check", duplicate.stderr)

    def test_json_report_can_be_written_atomically_to_a_new_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "nested" / "report.json"
            result = self.run_cli(
                "--base-url",
                self.base_url(),
                "--check",
                "models",
                "--output",
                output,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(report["summary"]["status"], "ok")
            self.assertFalse((output.parent / f".{output.name}.tmp").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)

