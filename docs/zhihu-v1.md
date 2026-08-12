# Local Codex Bridge V1：让 ChatGPT Actions 直接驱动你本地的 Codex（DeepSeek 后端）

> 状态：**初稿，未发布**。内容基于仓库当前真实代码与 2026-08-11/12 的本地测试记录撰写。
> 版本：1.0.0（pre-release，未 git init / push / tag）。

## TL;DR

一个零第三方依赖（Python 仅标准库）的本地 HTTP 桥：ChatGPT 通过 Custom GPT Actions 调用
`https://<你的 ngrok 域名>/...`，桥在你自己的机器上维持一个持久的 `codex app-server` 进程
（`model=deepseek-chat`、`model_provider=deepseek`），所有读写和命令都发生在**你的本地工作区**，
模型 key 不出你的机器。

## 为什么需要它

Codex CLI 天生是终端里一个人用的。想让 ChatGPT 变成"远程操作员"，缺四层东西：

1. **常驻 API**：每次开新会话的 CLI 不行，要一个一直活着的本地服务；
2. **控制通道**：能操纵已运行 Codex 的 stdio JSON-RPC（`codex app-server`）；
3. **会话连续性**：同一任务必须在同一个 native thread 上继续，而不是每次复制历史重来；
4. **安全边界**：ChatGPT 只拿到有限的动作 + Bearer key，本地审批流不暴露给它。

这个项目把四层都补齐了。

## 架构

![图片 1：整体架构图（ChatGPT → ngrok 隧道 → 本地 Bridge → codex app-server → DeepSeek）](images/placeholder-architecture.png)

```
ChatGPT (Custom GPT Actions)
   │  HTTPS + Authorization: Bearer <key>，x-openai-isConsequential: false
   ▼
ngrok 固定域名（.ngrok_domain，gitignored）
   │
   ▼
127.0.0.1:8321  BridgeHttpServer（http_server/，stdlib ThreadingHTTPServer）
   │  JSON-RPC 2.0 over stdio
   ▼
codex app-server（一个持久进程，model=deepseek-chat）
   │
   ▼
DeepSeek API（key 只在你的 CODEX_HOME / 环境变量里）
   │
   ▼
本地工作区（sandbox_mode=workspace-write，approval_policy=never）
```

关键点：

- 全链路只有一个 app-server 进程，`thread_id`/`turn_id` 是唯一会话句柄；
- 模型后端、API key、工作区全在你机器上，公网只暴露一个带 key 的 HTTP 面；
- `schemas/` 直接复用官方 app-server 协议 schema，不自己发明协议。

## V1 能力

| 能力 | 说明 |
|---|---|
| start / continue | 新建或继续 native thread，**不复制历史** |
| observe | 事件驱动等待（最长 10s，不轮询） |
| steer / interrupt | 排队注入指令 / 立即中断（Codex 0.147.0 语义） |
| list / read | 线程列表 / 真实历史（摘要化返回） |
| 安全 | 全端点 Bearer 认证（除 /health），常量时间比较 |
| 运维 | 幂等启动、精确 PID 管理、健康检查、stop 隔离 |

## 快速开始

前置：`codex` CLI 在 PATH（实测 codex-cli 0.147.0）、DeepSeek provider 已配置、
ngrok 已装且有固定域名、Python 3.8+。

```bash
openssl rand -hex 32 > .bridge_api_key && chmod 600 .bridge_api_key
echo 'your-name.ngrok-free.dev' > .ngrok_domain   # 或 export NGROK_DOMAIN=...
./scripts/start_ngrok_bridge.sh                   # 后台启动，输出 READY
curl -s http://127.0.0.1:8321/health
curl -s https://your-name.ngrok-free.dev/health
./scripts/stop_ngrok_bridge.sh                    # 只停本脚本启动的进程
```

然后把 `openapi.yaml`（模板）替换 URL 后粘贴进 Custom GPT → Actions，
认证选 API Key：Header `Authorization`，Value `Bearer <key>`。

![图片 2：Custom GPT Actions 配置界面（粘贴 OpenAPI + Bearer 认证）](images/placeholder-actions-config.png)

![图片 3：start_ngrok_bridge.sh 的 READY 启动输出](images/placeholder-ready-output.png)

## 安全模型：三个必须理解的权衡

### 1. `sandbox_mode="workspace-write"`

app-server 每次启动都显式传入（不依赖本机 config 文件）。含义：workspace 内的文件读写和命令
**直接执行**；workspace 外的一切**自动拒绝**。

### 2. `approval_policy="never"`

不会有任何审批弹窗——因为桥**不把审批请求转发给 ChatGPT**（需要权限提升的操作直接失败）。
好处是 ChatGPT 无法诱导出"提权"操作；代价是工作区外（如系统目录、网络）一律不可用，
没有升级通道。这是刻意的 V1 边界。

### 3. `x-openai-isConsequential: false`

每个 action 都声明为"非后果性"，ChatGPT 调用前不会弹确认。**这只是声明，不是保护**——
真正的边界是前两条 + Bearer key。持有 key ≈ 获得 workspace 内的命令执行权；
公网暴露时请严格控制 key 的分发与轮换。

## 测试历史（真实记录）

- 2026-08-11 集成测试全 PASS：core 5/5、actions 7/7、HTTP API 12 场景、公网 tunnel 6/6；
- 离线单元测试（`tests/test_config_propagation.py`，无需真实后端）：验证 app-server
  spawn 参数包含 `approval_policy="never"` 与 `sandbox_mode="workspace-write"`，且
  `CODEX_HOME` 正确透传到子进程环境；
- 启动脚本本身带三层健康检查：本地 `/health` → 隧道上线 → 公网 `/health`。

![图片 4：集成测试 PASS 结果摘要（2026-08-11）](images/placeholder-test-results.png)

## 已知限制

- 单常驻 app-server：无多租户/资源隔离；
- 无内置限流、无失败锁定、无 IP 白名单（依赖隧道层补充）；
- `steer` 是排队语义：不打断当前生成，需要立即转向请用 `interrupt` + `continue`；
- 响应摘要截断 4000 字符；`/threads` 单页 ≤20；
- 仅实测 macOS arm64 + codex 0.147.0；
- 暂无 MCP layer（`bridge/` 接口预留了扩展空间）。

## 后续计划

- git init + GitHub 仓库（可见性待定）与 LICENSE 选型；
- 本机复跑三个集成测试并归档结果；
- 隧道层访问控制/限流；
- 根据反馈考虑 MCP layer。

## 附录：仓库结构

```
bridge/        核心库（app-server 客户端 + BridgeCore）
http_server/   HTTP API（stdlib，Bearer 认证）
schemas/       codex app-server 协议 schema（v1/v2）
scripts/       start/stop_ngrok_bridge.sh（幂等、PID 管理）
tests/         3 个集成测试 + 1 个离线单测
openapi.yaml   公开模板（占位 URL）
docs/          知乎初稿等
```
