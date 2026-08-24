# Security Policy

## Supported versions

| Version | Security fixes |
|---|---|
| 1.x | Yes |
| 0.x | No |

仅最新的 `1.x` 版本会接收安全修复。报告问题前请先在最新版本复现。

## Private reporting

请通过本仓库的 **Security → Report a vulnerability** 私密提交安全问题，不要在公开 Issue、Discussion、日志或截图中粘贴：

- API Key、Authorization Header 或环境变量值；
- 可用的内部/私有 API Base URL；
- 未脱敏的模型回复、Prompt、请求正文或错误正文；
- 能直接复现漏洞但尚未协调披露的细节。

报告建议包含：

1. 受影响版本与 Python 版本；
2. 操作系统和完整命令行，但用占位符替换域名、路径和环境变量值；
3. 最小复现步骤与预期/实际结果；
4. 已脱敏的 JSON 报告；
5. 对 Key 泄露、跨主机重定向、报告脱敏或解析器资源消耗的影响说明。

如果仓库没有显示私密报告入口，请不要公开漏洞细节；等待维护者启用 GitHub Private Vulnerability Reporting。

## Secret exposure response

如果真实 API Key 已经进入命令历史、终端录屏、报告、Issue 或提交历史：

1. 立即在服务端撤销或轮换 Key；
2. 删除公开副本，但不要把“删除文件”当成撤销凭据；
3. 检查该 Key 的用量、来源 IP 和异常调用；
4. 用新 Key 重新运行，并确认报告中不含旧值；
5. 必要时清理 Git 历史与缓存副本。

本工具的脱敏是纵深防御，不替代服务端撤销、最小权限、额度限制与密钥轮换。

## Security design

- API Key 仅从用户指定的环境变量读取；
- CLI 不提供明文 Key 参数；
- Authorization Header 不写入报告；
- HTTP 重定向默认拒绝，避免跨地址转发 Bearer 凭据；
- TLS 使用系统/Python 默认信任链验证；
- 响应正文、错误正文、SSE 行、总字节和事件数量均有限制；
- 默认报告不包含 Prompt 和模型回复正文；
- 最终 JSON 会按真实 Key、Bearer 形态和常见密钥模式再次脱敏；
- 上游字符串先脱敏再截断，JSON 输出仅使用 ASCII 转义以兼容不同终端编码；
- 报告文件通过同目录临时文件和原子替换写入。

## Out of scope

- 上游 API 服务本身的漏洞或滥用问题；
- 仅由已撤销测试 Key 导致的 401；
- 用户主动使用 `--include-content` 后公开模型正文；
- 本地恶意管理员、调试器或同权限进程读取环境变量；
- 压力测试、流量放大或对第三方端点的扫描。

