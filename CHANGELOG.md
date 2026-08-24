# Changelog

本项目的重要变更记录在此文件中。格式参考 Keep a Changelog，版本号遵循 Semantic Versioning。

## [Unreleased]

### Changed

- 暂无。

## [1.0.0] - 2026-08-25

### Added

- 纯 Python 标准库的单文件 CLI；
- OpenAI-Compatible `GET /models` 探针与目标模型存在性检查；
- 非流式 `POST /chat/completions` JSON/Schema 检查；
- SSE 流式 Content-Type、UTF-8、事件 JSON、增量和 `[DONE]` 完整性检查；
- 严格拒绝 NaN、Infinity 与孤立 surrogate，并识别声明长度后的响应截断；
- 401/403/404/429/5xx、DNS、TLS、超时、JSON 与 SSE 错误分类；
- 仅从环境变量读取 API Key，并拒绝跨地址 HTTP 重定向；
- 默认不输出 Prompt 或回复正文的脱敏 JSON 报告；
- 上游错误、请求 ID 与响应元数据采用“先脱敏、后截断”；
- 显式 `--include-content`、重复 `--check` 和原子 `--output` 写入；
- 本地 mock server 回归测试。

