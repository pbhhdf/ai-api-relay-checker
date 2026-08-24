# AI API 中转站检测工具：OpenAI-Compatible API Relay Checker

一个**纯 Python 标准库**的命令行诊断器，用同一份 JSON 报告检查 OpenAI-Compatible API 的模型列表、非流式 Chat Completions 和 SSE 流式响应。它适合排查 API Base URL、Bearer 鉴权、模型 ID、401/403/404/429/5xx、JSON 协议和流式中断，不绑定任何特定服务商。

> **English:** A dependency-free CLI that probes `/models`, non-streaming chat completions, and SSE streaming on any OpenAI-compatible endpoint. It reads API keys only from environment variables and emits a redacted JSON report without response text by default.

## 为什么需要它

“能返回一段文字”不能证明一个 OpenAI 兼容端点完整可用。接入失败通常发生在不同层：

- Base URL 漏写或重复写入 `/v1`；
- API Key 没有按 `Authorization: Bearer ...` 发送；
- `/models` 可访问，但目标模型 ID 不在列表中；
- 非流式响应不是预期的 JSON 结构；
- `stream=true` 返回普通 JSON、非法 UTF-8、损坏的 SSE 事件，或在 `[DONE]` 前中断；
- 网关返回 429/5xx，而客户端只显示笼统的“请求失败”。

本工具把这些检查拆开记录，使报告可以复跑、对比和脱敏后提交给技术支持。

## 功能

| 检查 | 请求 | 核验内容 |
|---|---|---|
| `models` | `GET {base_url}/models` | HTTP 状态、JSON Content-Type、`data[].id` 结构、模型数量、目标模型是否存在 |
| `chat` | `POST {base_url}/chat/completions` | Bearer 鉴权、非流式 JSON、`choices[0].message.content`、结束原因、Token 用量 |
| `stream` | `POST {base_url}/chat/completions` | `text/event-stream`、UTF-8、SSE/JSON 事件、首事件耗时、内容增量、`[DONE]` 完整结束 |

安全默认值：

- API Key **只能**从环境变量读取，没有 `--api-key` 参数；
- 默认报告正文长度与 SHA-256，不输出模型回复正文；
- 已知密钥、Bearer Token 和常见密钥形态在最终 JSON 中二次脱敏；
- 不跟随 HTTP 重定向，避免把 Authorization Header 带到另一个地址；
- HTTPS 使用 Python 默认 CA 验证，不提供跳过证书校验的开关；
- JSON、错误正文、SSE 单行、总字节数和事件数均有上限。

## 环境要求

- Python 3.10 或更高版本；
- 无第三方依赖；
- 一个 OpenAI-Compatible API Base URL；
- 一个有最小测试额度的 API Key。

下载单文件后即可运行，不需要 `pip install`。

## 30 秒快速开始

Base URL 应该是 **API 前缀**，多数服务形如 `https://api.example.com/v1`。工具不会自动补 `/v1`，因为路径本身就是诊断对象。

### macOS / Linux

```bash
export OPENAI_API_KEY='your-key-here'
python3 ai_api_relay_checker.py \
  --base-url 'https://api.example.com/v1' \
  --model 'your-model-id' \
  --output report.json
```

### PowerShell

```powershell
$env:OPENAI_API_KEY = 'your-key-here'
python .\ai_api_relay_checker.py `
  --base-url 'https://api.example.com/v1' `
  --model 'your-model-id' `
  --output .\report.json
```

不传 `--output` 时，JSON 写到标准输出：

```bash
python3 ai_api_relay_checker.py \
  --base-url 'https://api.example.com/v1' \
  --model 'your-model-id'
```

全部检查通过时退出码为 `0`；探针失败为 `1`；参数、环境变量或报告写入错误为 `2`。

## 只运行部分检查

`--check` 可以重复。只查询模型列表时不要求 `--model`：

```bash
python3 ai_api_relay_checker.py \
  --base-url 'https://api.example.com/v1' \
  --check models
```

只测试两种 Chat 响应：

```bash
python3 ai_api_relay_checker.py \
  --base-url 'https://api.example.com/v1' \
  --model 'your-model-id' \
  --check chat \
  --check stream
```

使用非默认环境变量名：

```bash
export RELAY_API_KEY='your-key-here'
python3 ai_api_relay_checker.py \
  --base-url 'https://api.example.com/v1' \
  --model 'your-model-id' \
  --api-key-env RELAY_API_KEY
```

查看完整参数：

```bash
python3 ai_api_relay_checker.py --help
```

## JSON 报告

默认报告不包含 `prompt` 和模型回复正文。下面是精简示例，实际报告还会记录每项检查的耗时、Content-Type、请求 ID、事件计数和安全截断标志：

```json
{
  "schema_version": "1.0",
  "tool": {
    "name": "ai-api-relay-checker",
    "version": "1.0.0"
  },
  "configuration": {
    "api_key_env": "OPENAI_API_KEY",
    "base_url": "https://api.example.com/v1",
    "checks": ["models", "chat", "stream"],
    "include_content": false,
    "model": "your-model-id",
    "prompt_chars": 22,
    "timeout_seconds": 15.0
  },
  "summary": {
    "checks_total": 3,
    "failed": 0,
    "passed": 3,
    "status": "ok"
  }
}
```

`summary.status` 有三种值：

- `ok`：所有选择的检查通过；
- `degraded`：部分通过、部分失败；
- `failed`：所有选择的检查都失败。

`content_sha256` 用来比较两次响应是否变化，不等于保存正文。只有显式添加 `--include-content` 时，报告才会加入最多 4096 个字符的 `content` 字段：

```bash
python3 ai_api_relay_checker.py \
  --base-url 'https://api.example.com/v1' \
  --model 'your-model-id' \
  --include-content \
  --output report-with-content.json
```

带正文的报告可能包含提示词相关数据，不应直接公开。

## 错误分类与处理

| `error.kind` | 常见原因 | 下一步 |
|---|---|---|
| `authentication_error` | 401/403、Key 无效、权限不足 | 核对环境变量、Key 状态和模型权限 |
| `not_found` | 404、Base URL 或路径错误 | 检查是否漏写/重复写入 `/v1` |
| `rate_limited` | 429、额度或并发限制 | 查看 `retry_after`，降低频率并核对余额 |
| `server_error` | 网关或上游 5xx | 保存 UTC 时间与 `request_id` 后重试 |
| `redirect_rejected` | 端点返回 3xx | 直接填写最终 API 地址，不通过跳转传 Key |
| `timeout` | 建连、读取或流式事件超时 | 增大 `--timeout`，再分时段复测 |
| `dns_error` / `tls_error` | DNS、证书链或 TLS 握手失败 | 检查域名解析、系统时间和证书链 |
| `unexpected_content_type` | JSON 接口返回 HTML；流式接口返回普通 JSON | 检查反向代理、WAF 和 `stream=true` 支持 |
| `invalid_json` / `invalid_schema` | 返回体损坏或兼容结构不完整 | 对照服务商协议与网关转换日志 |
| `api_error` | HTTP 200 中仍包含 API 错误对象 | 查看事件/响应中的错误消息和网关日志 |
| `invalid_sse_json` / `invalid_sse_schema` | SSE `data:` 事件不是合法 Chat chunk | 检查流式协议适配层 |
| `sse_incomplete` | 连接在 `[DONE]` 前断开 | 检查代理缓冲、空闲超时和上游中断 |
| `model_not_listed` | 指定模型不在 `/models` | 以实时模型 ID 为准，不用展示名猜测 |

HTTP 错误正文会先按 API Key 与 Prompt 脱敏，再截取最多 300 个字符的诊断消息；避免长密钥被截断后残留前缀。

## 参数校验

工具会在发请求前拒绝以下输入：

- 非 `http://` / `https://` 的 Base URL；
- 未先做 IDNA/百分号编码的非 ASCII 域名或路径；
- 带用户名、密码、Query 或 Fragment 的 Base URL；
- 已经指向 `/models` 或 `/chat/completions` 的地址；
- 重复的 `--check`、非法环境变量名、空模型 ID；
- 小于 0.1 秒或大于 120 秒的超时；
- 超过 4096 字符的 Prompt，或不是无空白可打印 ASCII 的 API Key。

这能减少路径拼接错误、Header 注入和误把密钥写进命令历史的风险。

## 测试

测试只启动本机随机端口的 mock server，不访问外部 API：

```bash
python3 -m unittest discover -s tests -v
```

覆盖范围包括成功路径、401、429、503、超时、Content-Type、非法 JSON/NaN/孤立 surrogate、错误 Schema、模型缺失、响应截断、SSE 中断、BOM/CR 换行、损坏事件、非法 UTF-8、API 错误事件、长 Key/Prompt 脱敏、ASCII stdout 和原子写报告。

## 结果解读边界

- 单次成功只能证明“这个时间点、这个 Key、这个模型、这组最小请求”可用，不代表长期 SLA；
- `/models` 的 ID 与响应行为不能单独证明底层模型来源或身份；
- 首事件耗时包含本地网络、网关排队和上游生成时间，不应直接当成服务端纯计算耗时；
- 工具不执行压力测试、计费复算、Function Calling、视觉输入或 Responses API 验证。

如果需要进一步设计可重复的稳定性、协议与账单测试，可阅读 [AI API 中转站检测方法](https://kaigpt.vip/blog/api-zhongzhuan-testing-guide/)；该链接只作为与本工具直接相关的延伸资料。

## 安全与版本

- 提交漏洞前请阅读 [SECURITY.md](SECURITY.md)；
- 版本变化见 [CHANGELOG.md](CHANGELOG.md)；
- 本项目使用 [MIT License](LICENSE)。

