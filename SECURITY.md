# Security

## 密钥管理

- `.bridge_api_key`：`openssl rand -hex 32` 生成（256-bit），`chmod 600`，已 gitignore。
- 只通过 `BRIDGE_API_KEY` 环境变量注入运行中的进程，从不写入脚本、日志或 pid 文件
  （写入即视为 bug；仓库日志已验证无密钥命中）。
- 轮换：重新生成文件 → 重启 bridge → 同步更新 ChatGPT Actions 中的 key。
- 运行产物（`.public_url`、`.ngrok_domain`、`.runtime/`、`*.pid`、`*.log`）全部 gitignored。

## 威胁模型

- 除 `GET /health` 外，所有端点要求 `Authorization: Bearer <BRIDGE_API_KEY>`
  （`hmac.compare_digest` 常量时间比较）。
- app-server 固定以 `approval_policy="never"` + `sandbox_mode="workspace-write"` 启动：
  - 允许：workspace 内文件读写、命令执行，无需人工确认；
  - 拒绝：workspace 外的一切操作（自动 denied），无升级通道；
  - bridge 不转发任何 `requestApproval` 类请求给 ChatGPT。
- **持有 Bearer key ≈ 获得 workspace 内的命令执行权**。公网暴露时请严格控制 key 分发，
  并定期轮换。
- `x-openai-isConsequential: false` 只是向 ChatGPT 的声明（调用前不弹确认），
  不是安全控制；真正的边界是 key 认证 + 工作区沙箱。

## 部署建议

- 隧道域名是个人 ngrok 账号资产，存于 `.ngrok_domain`（gitignored），不要提交。
- 当前版本无内置限流、无失败锁定、无 IP 白名单；长期公网使用建议在隧道/网关层补充。
- 运行时日志包含 thread 内容与命令文本，不要在日志中写入秘密。
- `/health` 无认证是有意设计（供隧道与探测），不返回敏感信息。

## 报告

- 项目已公开（`Xin-Jiaqi/local-codex-bridge`）。请通过 GitHub issues 报告安全问题；
  不要在 issue 中粘贴任何密钥。
