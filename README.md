# Local Codex Bridge

把 ChatGPT 接进你本地 Codex CLI 的桥：ChatGPT 通过 Custom GPT Actions 调用一组 HTTP 接口，桥在你自己的机器上启动一个持久的 `codex app-server` 进程，在**同一个 native Codex thread** 里完成 start / continue / observe / steer / interrupt / list / read，模型走你自己的 DeepSeek 配置，读写都在本地工作区。

- Python 3.8+，**零第三方依赖**（仅标准库，无 `requirements.txt`）
- 无 MCP layer：V1 就是一个本地 HTTP 桥 + JSON-RPC over stdio 客户端

## 为什么需要

Codex CLI 原本是终端里一个人用：你敲命令、看输出。要把 ChatGPT 变成"远程操作员"，缺的层是：

1. 一个常驻的本地 API，而不是每次开新会话；
2. 一个能"控制"已运行 Codex 的通道（`codex app-server` 的 stdio JSON-RPC）；
3. 会话的连续性：同一任务必须在同一个 native thread 上继续，不能每次复制历史重新开始；
4. 安全边界：ChatGPT 只拿到有限的动作和 key，且不需要它替你做本地审批。

这个项目补齐的就是这四层。

## 架构

```
ChatGPT (Custom GPT)
   │  Actions 调用 (Bearer API Key, x-openai-isConsequential: false)
   ▼
ngrok 固定域名 (https://<your-domain>.ngrok-free.dev)
   │  HTTPS → 127.0.0.1:8321
   ▼
Local Codex Bridge HTTP API  (http_server/, Python stdlib ThreadingHTTPServer)
   │  JSON-RPC 2.0 over stdio (newline-delimited JSON)
   ▼
codex app-server  (一个持久进程；model=deepseek-chat, model_provider=deepseek)
   │  native thread / turn
   ▼
Native Codex Thread → Model Backend (DeepSeek, 你的 DEEPSEEK_API_KEY)
   ▼
Local Workspace (sandbox_mode=workspace-write, approval_policy=never)
```

要点：

- 全链路只有一个持久 `codex app-server` 进程，Bridge 与它是一对一连接；
- `thread_id` / `turn_id` 就是会话句柄，ChatGPT 不需要理解 Codex 内部格式；
- 模型后端、API key、工作区全部在你的机器上，公网只暴露一个带 key 的 HTTP 面。

## V1 能力

| 能力 | 说明 |
|---|---|
| `start` | 新建 native thread 并开始第一轮（可指定 cwd） |
| `continue` | 同一 thread 继续，**不复制历史**，模型记得前文 |
| `observe` | 有界、事件驱动等待 turn 结束（最长 10s，不轮询） |
| `steer` | 向运行中的 turn 排队注入新指令（Codex 0.147.0 语义，不打断生成） |
| `interrupt` | 中止运行中的 turn |
| `list` | 列出 native threads（含 cwd/preview/status/updated_at） |
| `read` | 读取 thread 真实 turn 历史（摘要化返回） |
| 本地读写 / shell | 工作区内读写与命令直接执行，无审批弹窗 |
| Bearer API Key | 除 `/health` 外全部接口认证 |
| workspace-write + approval_policy=never | 显式传给 app-server 的安全边界 |
| 后台启动 / PID 管理 | `start_ngrok_bridge.sh` 记录 PID，`stop_ngrok_bridge.sh` 只杀自己启动的进程 |
| 开机自启 (LaunchAgent) | 登录自动启动，幂等复用 `.runtime` PID 机制，卸载不影响运行中进程 |
| health checks | 启动时依次验证本地 `/health`、tunnel 上线、公网 `/health` |
| 完整生命周期 | stop → start → health 可重复，幂等 |

## Quick Start

前置条件：

- `codex` CLI 在 PATH（本机为 `codex 0.147.0`，macOS arm64 实测）
- DeepSeek provider 已配置（见下文「Codex / DeepSeek 配置」）
- `ngrok` 已安装、已 `ngrok config add-authtoken`、有一个固定域名
- Python 3.8+

```bash
# 1. API key（只在进程环境变量里使用，绝不入库）
openssl rand -hex 32 > .bridge_api_key && chmod 600 .bridge_api_key

# 2. 你的 ngrok 固定域名（写入 gitignored 文件，或 export NGROK_DOMAIN=...）
echo 'your-name.ngrok-free.dev' > .ngrok_domain

# 3. 启动 bridge + ngrok（后台、幂等）
./scripts/start_ngrok_bridge.sh

# 4. 验证
curl -s http://127.0.0.1:8321/health          # {"status":"ok",...}
curl -s https://your-name.ngrok-free.dev/health

# 5. 停止（只停本脚本启动的进程）
./scripts/stop_ngrok_bridge.sh
```

不带公网暴露的本地模式：

```bash
export BRIDGE_API_KEY="$(cat .bridge_api_key)"
python3 -m http_server --host 127.0.0.1 --port 8321
```

## 开机自启（LaunchAgent）

登录 macOS 后自动后台启动 Bridge + ngrok：LaunchAgent 在登录时执行一次 `scripts/start_ngrok_bridge.sh`，该脚本幂等，会复用 `.runtime/` 里仍在运行的 PID，不会重复启动；停止仍用 `scripts/stop_ngrok_bridge.sh`。

```bash
# 安装：生成 ~/Library/LaunchAgents/com.local.codex-bridge.plist 并立即加载
./scripts/install_launch_agent.sh

# 查看状态（agent 是否加载、bridge/ngrok PID、本地/公网健康检查）
./scripts/status_launch_agent.sh

# 卸载：默认只移除开机自启、不碰正在运行的进程；--stop 才会同时停服务
./scripts/uninstall_launch_agent.sh
./scripts/uninstall_launch_agent.sh --stop
```

说明：

- **模板在仓库、生成在本机**：plist 模板是 `scripts/launch_agent/com.local.codex-bridge.plist`，安装时把占位符 `__PROJECT_ROOT__` / `__HOME__` 替换为绝对路径后生成到 `~/Library/LaunchAgents/`，仓库里不落任何 secret 或本机路径。
- **launchd 的短 PATH**：plist 注入了常见 bin 目录（`~/.local/bin`、`/opt/homebrew/bin`、`/usr/local/bin` 等）；`start_ngrok_bridge.sh` 也会在 PATH 找不到时自动去这些目录找 `codex` / `ngrok`，其余工具均在 `/usr/bin`、`/bin` 内。
- **不重复启动**：`RunAtLoad` 登录只跑一次、`KeepAlive=false` 不自动重启；start 脚本本身幂等 + `.runtime` PID 校验 + 单实例锁（`.runtime/start.lock`），并发调用也只会有一个生效。
- **进程不被 launchd 回收**：plist 设置 `AbandonProcessGroup=true`，start 脚本退出后，后台的 bridge/ngrok 继续独立运行，`launchctl bootout` 卸载 agent 也不会误杀它们。
- **日志**：launchd 的 stdout/stderr 写到 `.runtime/launchagent.out.log` / `launchagent.err.log`（gitignored）。
- **重试**：如果登录时网络未就绪导致启动失败，可手动 `launchctl kickstart gui/$(id -u)/com.local.codex-bridge` 或直接 `./scripts/start_ngrok_bridge.sh` 重试；修改模板后可用 `./scripts/install_launch_agent.sh --force` 重新加载。

## Custom GPT Actions 配置

1. 使用仓库里的 `openapi.yaml` 模板（servers URL 是占位符 `https://REPLACE_WITH_PUBLIC_URL`，把它替换成你自己的 ngrok 域名）。仓库里的 `openapi.ngrok.yaml` 是本机真实部署副本，已在 `.gitignore` 中，不随仓库发布。
2. 在 ChatGPT → Custom GPT → Actions → 粘贴替换后的 OpenAPI schema。
3. Authentication 选择 **API Key**：Header 名 `Authorization`，Value 为 `Bearer <你的 .bridge_api_key 内容>`。
4. schema 中所有 action 已设 `x-openai-isConsequential: false`，所以 ChatGPT 不会每次调用都弹出确认（无重复 Allow）。安全由 Bridge 层保证：key 认证 + 工作区沙箱 + 拒绝工作区外操作。
5. GPT 端超时约束：`observe` 单次上限 10s，需要长任务时组合多次 `start → observe → observe → … → continue`，而不是一次长等待。

## Codex / DeepSeek 配置

Bridge 启动的 app-server 使用独立的 `CODEX_HOME`（默认 `~/.codex-deepseek`，可通过环境变量覆盖）。该 profile 里需要定义 DeepSeek provider：

```toml
# $CODEX_HOME/config.toml（示例，按你的 Codex 版本调整）
model_provider = "deepseek"
model = "deepseek-chat"
model_reasoning_effort = "max"
```

provider 定义（通常 `~/.codex/providers/deepseek.toml` 或 `$CODEX_HOME` 下）：

```toml
base_url = "https://api.deepseek.com"
env_key = "DEEPSEEK_API_KEY"
wire_api = "responses"
```

`DEEPSEEK_API_KEY` 只需要存在于启动 bridge 的 shell 环境中（脚本会透传），key 本身不写入任何项目文件。

## 7 个 Bridge Actions

| HTTP | Action | 说明 |
|---|---|---|
| `GET /health` | health | 免认证探活，返回 app-server 状态与模型配置 |
| `POST /start` | start | `{prompt, cwd?}` → 新建 native thread 并开始第一轮 |
| `POST /continue` | continue | `{thread_id, prompt}` → 同一 thread 新 turn |
| `POST /observe` | observe | `{thread_id, turn_id, wait_ms?}` → 事件驱动等待（≤10000ms） |
| `POST /steer` | steer | `{thread_id, turn_id, prompt}` → 排队注入指令，不打断生成 |
| `POST /interrupt` | interrupt | `{thread_id, turn_id}` → 中止并返回最终状态 |
| `GET /threads` | list | 列出 native threads（limit ≤20） |
| `GET /threads/{thread_id}` | read | 读取 thread 真实历史（最多 20 个 turn，摘要截断 4000 字符） |

错误格式统一为 `{"error": {"type": ..., "message": ...}}`：400 参数错误、401 未认证、404 未知 thread/turn、413 body 过大、502 app-server 错误。

## native Codex thread 设计原则

- **一个持久连接**：Bridge 只 spawn 一个 `codex app-server`（stdio JSON-RPC），HTTP 层共享它，不存在多份会话数据库。
- **continuation 而不是复制**：`continue` 先 `thread/read` 确认 thread 存在，再 `turn/start` 同一 thread；模型上下文来自 Codex 原生 turn 历史。
- **事件驱动等待**：`observe` 订阅 `turn/completed` 通知（`threading.Event`），不做忙轮询；超时返回 `running` 由调用方决定下一步。
- **steer 前置校验**：`turn/steer` 带 `expectedTurnId`，若服务端返回不同 turn 立即报错，绝不静默漂移。
- **永不挂起**：server 主动请求（如权限询问）统一回 `-32601`；进程退出/管道断开时一次性失败所有 pending 请求。
- **线程即状态**：thread/turn id 是唯一会话句柄，bridge 不维护自己的会话表，重启后历史仍在 Codex 原生存储里。

## 安全边界

- **API Key**：`openssl rand -hex 32`（256-bit）；`hmac.compare_digest` 常量时间比较；key 只经 `BRIDGE_API_KEY` 环境变量注入，写进日志/PID 文件即视为 bug。
- **沙箱与审批**：app-server 每次启动都显式传 `-c 'approval_policy="never"'` 和 `-c 'sandbox_mode="workspace-write"'`（不依赖本机 config 文件），工作区内读写/命令无提示执行，工作区外操作自动拒绝。
- **无交互审批流**：Bridge 不转发任何 permission prompt 给 ChatGPT；需要权限提升的操作直接失败。
- **PID 隔离**：start 脚本把 PID 记入 `.runtime/`，stop 只 kill 记录中的 PID；端口/域名被未托管进程占用时报错退出、绝不动它。
- **不入库清单**：`.bridge_api_key`、`.public_url`、`.ngrok_domain`、`openapi.ngrok.yaml`（含真实域名）、`.runtime/`、所有 `*.log`、`*.pid`、`__pycache__/` 均在 `.gitignore`。
- **本地优先**：HTTP 默认只绑 `127.0.0.1`，公网面只经过 ngrok 与 key 认证。

## 已知限制

- `approval_policy=never` 意味着任何需要提权的操作（工作区外写入、网络请求）直接失败，没有升级通道——这是刻意的 V1 边界。
- `steer` 在 Codex 0.147.0 中是排队语义：不打断当前生成，当前回复结束后才注入。需要立即转向请用 `interrupt` + `continue`。
- `observe` 单次最多 10s（GPT Actions 超时约束），长任务需要调用方循环等待。
- `assistant_text` 摘要截断（4000 字符）；`/threads` 单页 ≤20。
- 会记录 `Model metadata for deepseek-chat not found` warning（仅记录，不致命）。
- 运行时日志包含 thread 内容与命令文本，全部 gitignored，不要在日志里放秘密。
- 仅在 macOS arm64 + `codex 0.147.0` 实测；Windows/Linux 与其它 codex 版本未验证。
- 暂无 MCP layer；`bridge/` 的接口设计预留了未来在其上构建 MCP 的可能。

## 项目结构

```
bridge/                核心库
  client.py            CodexAppServerClient：spawn/shutdown app-server、JSON-RPC 收发、
                       通知分发、进程退出处理
  core.py              BridgeCore：start/continue/observe/steer/interrupt/list/read
http_server/           HTTP API（stdlib ThreadingHTTPServer，Bearer 认证）
  server.py            路由、参数校验、错误格式、openapi 校验点
scripts/
  start_ngrok_bridge.sh   后台启动 bridge + ngrok（幂等、PID 记录、health 检查）
  stop_ngrok_bridge.sh    只停止本脚本启动的进程
  install_launch_agent.sh   生成并加载 LaunchAgent（登录自启；--force 重新加载）
  uninstall_launch_agent.sh 移除 LaunchAgent（--stop 可选同时停服务）
  status_launch_agent.sh    查看自启与运行状态（agent/PID/健康）
  launch_agent/             LaunchAgent plist 模板（安装时生成到 ~/Library/LaunchAgents）
tests/
  test_config_propagation.py  离线单测：spawn 参数包含 approval/sandbox override
  test_bridge_core.py         集成测试：start/continue/observe/interrupt/进程退出（5 场景）
  test_bridge_actions.py      集成测试：7 个 action（含 steer 排队语义）
  test_http_api.py            集成测试：HTTP API + openapi 校验（12 场景）
openapi.yaml            公共 Actions 模板（servers URL 为占位符）
schemas/                codex app-server 协议 JSON Schema（v1/v2，参考用）
```

## V1 状态

- 版本 `1.0.0`（`bridge.__version__`、`openapi.yaml`、app-server initialize clientInfo 一致）。
- 离线单测：`python3 tests/test_config_propagation.py`（2 项，无需真实 app-server）。
- 集成测试需要真实 app-server + DeepSeek key：`python3 tests/test_bridge_core.py`、`python3 tests/test_bridge_actions.py`、`python3 tests/test_http_api.py`。最近一次完整记录（2026-08-11）为全 PASS：core 5/5、actions 7/7、HTTP API 11/11、公网 tunnel 6/6。
- 已实测能力：start/continue/observe/steer/interrupt/list/read、本地读写、shell、native thread 连续工作、Bearer API Key、workspace-write + approval_policy=never、Bridge/ngrok 后台启动、PID 管理、stop 隔离、health checks、完整 stop→start→health 生命周期。
