# 把 ChatGPT 接到本地 Codex CLI：从复制命令到一句话直接操作本地项目

> 状态：已随 v1.0.0 发布（`Xin-Jiaqi/local-codex-bridge`，BSD-3-Clause）。
> 本文基于 2026-08 的仓库代码、测试记录与真实开发历史整理。

## 一、起点：复制粘贴循环

用 Codex CLI 干活，默认流程是终端里一人一机：把任务敲进去，等输出，再手动搬运结果。
这个循环在单机场景下成立，但把 ChatGPT 加进来之后，缺的东西变得具体：ChatGPT 需要一条
稳定的通道去调用本地 Codex，一段提示词解决不了这个需求。这个项目就是这条通道：
一个零第三方依赖（Python 标准库）的本地 HTTP 桥，把 Custom GPT Actions 接到常驻的
`codex app-server` 上，模型走你自己的 DeepSeek 配置。

![图片 1：自然语言实际效果——在 ChatGPT 里用一句话启动一个本地任务，任务在本地 workspace 执行并返回结果](images/placeholder-natural-language.png)

## 二、常驻的 app-server

`codex app-server` 提供一个基于 stdio 的 JSON-RPC 协议（`codex app-server --listen stdio://`），
一行一个 JSON 帧。Bridge 用 subprocess 拉起一个持久进程，完成 `initialize` 握手后保持连接：

```bash
codex app-server --listen stdio:// \
  -c 'model="deepseek-chat"' -c 'model_provider="deepseek"' \
  -c 'approval_policy="never"' -c 'sandbox_mode="workspace-write"'
```

协议 schema 直接复用官方 `schemas/`（v1/v2），不自己发明格式。`CODEX_HOME` 指向独立
profile（默认 `~/.codex-deepseek`），DeepSeek provider 与 key 都在这个 profile 里，
key 只存在于启动 bridge 的 shell 环境中，不写入任何项目文件。

## 三、native thread：会话的唯一事实来源

Codex 的会话模型以 thread / turn 为句柄。Bridge 的会话连续性依赖一条规则：`continue`
永远在同一个 native thread 上开新 turn（先 `thread/read` 确认存在，再 `turn/start`），
不复制历史、不重建上下文。Bridge 自己不维护会话表，thread 的完整历史存在 Codex 原生
存储里，bridge 重启后依然可读。调用方只需要记住两个 id：`thread_id` 和 `turn_id`。

## 四、七个动作的抽象

协议被收敛成 7 个动作：

- `start`：新建 native thread 并开始第一轮（可指定 cwd）
- `continue`：同一 thread 继续，模型记得前文
- `observe`：有界、事件驱动地等待 turn 结束
- `steer`：向运行中的 turn 排队注入新指令
- `interrupt`：中止运行中的 turn
- `list`：列出 native threads（含 cwd / preview / status / updated_at）
- `read`：读取 thread 的真实 turn 历史（摘要化返回）

HTTP 层对应 8 个端点（`/health` 免认证 + 上述 7 个 action），错误统一为
`{"error": {"type": ..., "message": ...}}`：400 参数错误、401 未认证、404 未知
thread/turn、413 body 过大、502 app-server 错误。

![图片 2：整体架构图（ChatGPT → ngrok 隧道 → 本地 Bridge → codex app-server → DeepSeek）](images/placeholder-architecture.png)

## 五、observe：事件驱动，不轮询

`observe` 订阅 `turn/completed` 通知，配合 `threading.Event` 做有界等待：turn 结束立刻
返回，超时返回 `running`，由调用方决定下一步。没有忙轮询。GPT Actions 端有超时约束，
单次 observe 上限 10s；长任务用 `start → observe → observe → … → continue` 的组合跑完，
每轮 observe 拿一次增量结果。

## 六、steer 与 interrupt：0.147.0 的真实语义

`turn/steer` 在 Codex 0.147.0 里是排队语义：指令注入当前正在运行的 turn，不打断生成，
当前回复结束后才生效。steer 请求带 `expectedTurnId`，如果服务端返回了不同的 turn，
bridge 直接报错，避免静默漂移。要立即转向，正确组合是 `interrupt` 中止当前 turn，
再 `continue` 同一 thread 开新 turn。这套语义也写进了 `openapi.yaml` 的 steer 描述里，
调用方和实现保持一致。

## 七、HTTP / OpenAPI / Custom GPT

Bridge 绑定 `127.0.0.1:8321`（stdlib `ThreadingHTTPServer`），`openapi.yaml`（3.1.0）
是公开模板，`servers.url` 是占位符。使用流程：把模板里的 URL 替换成自己的 ngrok 域名，
粘贴进 Custom GPT → Actions，认证方式选 API Key（Header `Authorization`，
Value `Bearer <key>`）。部署副本（`openapi.ngrok.yaml`）含真实域名，gitignored，
不随仓库发布。

![图片 3：Custom GPT Actions 配置界面（粘贴 OpenAPI schema + Bearer 认证）](images/placeholder-actions-config.png)

## 八、隧道：从 Cloudflare 到 ngrok

早期版本用 cloudflared quick tunnel 暴露公网（`start_public_bridge.sh`，v1.0.0 清理时
移除）。quick tunnel 的问题在运维层：URL 每次重启都会变，免费档连接不稳定。期间遇到过
一个典型的误判场景：本地 `/health` 正常、observe 按预期等待，公网请求却超时——第一反应
像是 observe 的 bug，排查后确认是隧道断连（tunnel connectivity failure），本地观察
逻辑本身没有问题。

v1.0.0 换用 ngrok 固定域名：域名写进 `.ngrok_domain`（gitignored），启动脚本幂等拉起
bridge 与 ngrok，并做三层健康检查——本地 `/health`、隧道上线（ngrok 本地 API）、公网
`/health`。任何一层不过就报错退出，不留下"半活"状态。

## 九、认证与 consequential

除 `/health` 外全部端点要求 `Authorization: Bearer <key>`，用 `hmac.compare_digest`
做常量时间比较；key 是 256-bit 随机值（`openssl rand -hex 32`），只经环境变量注入进程。
`x-openai-isConsequential: false` 的作用是让 ChatGPT 调用前不弹确认，它只是声明，
安全边界由 key 认证与工作区沙箱构成。持有 key 相当于拿到 workspace 内的命令执行权，
公网暴露时控制分发、定期轮换。

## 十、approval 与 workspace-write

app-server 固定以 `approval_policy="never"` + `sandbox_mode="workspace-write"` 启动，
不依赖本机 config 文件。效果：workspace 内的文件读写与命令直接执行；workspace 外的
一切自动拒绝。bridge 不转发 `requestApproval` 类请求（统一回 `-32601`），所以没有任何
审批弹窗，也没有升级通道。代价是系统目录、网络等需要提权的操作一律不可用——这是刻意的
V1 边界。如果把这两个 override 去掉，app-server 会回落到 config 里的审批策略（比如
`untrusted`），每个命令都触发 `requestApproval`，bridge 只能回 `-32601`，表现为
"approval request failed"。这也是 override 写死在代码里的原因。

## 十一、生命周期与开机自启

start / stop 脚本用 `.runtime/` 里的 PID 文件做精确管理：只杀自己启动的进程，端口或
域名被未托管进程占用时直接报错、绝不动它；start 脚本幂等，重复执行会复用仍在运行的
进程。LaunchAgent 把同一套脚本接到登录自启：plist 模板放在仓库里（占位符安装时替换成
绝对路径，生成到 `~/Library/LaunchAgents/`），`RunAtLoad` 登录启动一次，`KeepAlive=false`
不自动重启，`AbandonProcessGroup` 保证脚本退出后后台进程不被 launchd 回收。launchd 的
PATH 很短，plist 注入常见 bin 目录，start 脚本也会自动在 `~/.local/bin`、
`/opt/homebrew/bin`、`/usr/local/bin` 里找 `codex` 和 `ngrok`。

![图片 4：start_ngrok_bridge.sh 的 READY 启动输出（三层健康检查通过）](images/placeholder-ready-output.png)

## 十二、最终体验

在 ChatGPT 里说一句话，比如"把当前仓库里所有 TODO 找出来，按文件整理成清单"：ChatGPT
调 `/start` 建 thread，任务在本地 workspace 由 codex 执行，`observe` 拿结果；中途要改
方向就 `steer`，要立刻停就 `interrupt`。全程模型 key、工作区、进程都在自己的机器上，
公网只暴露一个带 key 的 HTTP 面。ChatGPT 拿到的只有 7 个动作和认证后的响应摘要，
看不到本地文件系统，也不能绕开沙箱。

## 十三、限制与适用人群

- 单常驻 app-server，无多租户与资源隔离
- 无内置限流、失败锁定、IP 白名单，长期公网使用需在隧道/网关层补充
- `steer` 是排队语义，需要立即转向请用 `interrupt` + `continue`
- 响应摘要截断 4000 字符；`/threads` 单页 ≤20
- 仅实测 macOS arm64 + codex 0.147.0，其它平台与版本未验证
- 暂无 MCP layer，`bridge/` 接口为后续扩展留了位置

适用人群是个人开发者：把 ChatGPT 当"远程操作员"，在自己的机器上跑单机任务。
多用户共用、需要审批流或审计的场景不适合当前形态。

测试数字口径：离线单测 2/2（`test_config_propagation.py`，当前可复现）；集成测试
2026-08-11 实测 core 5/5、actions 7/7、HTTP API 11/11、公网 tunnel 6/6（历史记录）；
当前 `test_http_api.py` 含 12 个场景，与历史记录差 1 项，未复跑确认。

整个系统只有两条边界：本地一条 JSON-RPC 通道，公网一个带认证的 HTTP 面。会话连续性
与安全边界写死在代码里，剩下的都是运维细节。
