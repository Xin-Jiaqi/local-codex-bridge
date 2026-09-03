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
Local Workspace (sandbox_mode=workspace-write, approval_policy=on-request)
```

要点：

- 全链路只有一个持久 `codex app-server` 进程，Bridge 与它是一对一连接；
- `thread_id` / `turn_id` 就是会话句柄，ChatGPT 不需要理解 Codex 内部格式；
- 模型后端、API key、工作区全部在你的机器上，公网只暴露一个带 key 的 HTTP 面。
- dual-host-router 分支（可选）让**同一个 GPT 同时控制 Mac + Windows**：Mac 保留
  固定公网 URL 做路由器，Windows 只做出站 worker、不开公网（见「Windows：一
  GPT 双机」）；默认不开启时一切行为不变。

**控制面 vs 任务面（1.1.0）**：控制面（实例配置、运行状态、LaunchAgent）与任务面
（ChatGPT 驱动的任务工作区）显式分离。控制面状态存放在任务工作区之外
（`${XDG_STATE_HOME:-$HOME/.local/state}/local-codex-bridge/<instance>/`，
目录 `700`、配置 `600`），仓库内没有可切换的全局 profile 指针；任务 cwd 被限制
在普通项目目录，不能选 `$HOME`、本 Bridge 仓库或控制面路径当工作区（见
「实例：local / hpc / maintenance（控制面隔离）」与「安全边界」）。

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
| workspace-write + approval_policy=on-request | 显式传给 app-server 的安全边界（实例模板同值；`BRIDGE_APPROVAL_POLICY` 可 pin `never`） |
| 后台启动 / PID 管理 | `start_ngrok_bridge.sh` 记录 PID，`stop_ngrok_bridge.sh` 只杀自己启动的进程 |
| 开机自启 (LaunchAgent) | 登录自动启动，幂等复用 `.runtime` PID 机制，卸载不影响运行中进程 |
| health checks | 启动时依次验证本地 `/health`、tunnel 上线、公网 `/health` |
| 完整生命周期 | stop → start → health 可重复，幂等 |

## Quick Start

前置条件：

- `codex` CLI 在 PATH（实测环境：macOS arm64 + `codex 0.147.0`）
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

## Windows：一 GPT 双机（dual-host-router）

一句话：**同一个 Custom GPT（同一个 Action URL、同一把 API key）可以同时控制
Mac 和 Windows 两台机器**。Mac 上固定 ngrok URL 与全部公开 HTTP API 保持
不变；Windows **不开公网、不抢 ngrok 域名**，只装一个"出站 worker"，主动去
连 Mac 的路由器领活、在本机执行、把结果交回去。换机器 = 换 `/start` 里的
`cwd` 路径，仅此而已。

### 路由规则（GPT 视角，用人话）

- `/start` 给 Windows 原生路径（`C:\...`、`D:\...`、UNC `\\server\share`，
  含 WSL 挂载 `\\wsl$\<发行版>` / `\\wsl.localhost\<发行版>`，正斜杠形式
  `//server/share` 同样识别）→ 新任务在 **Windows** 上跑；
- `/start` 给 macOS POSIX 路径（`/Users/...`）或不给 `cwd` → 和以前一样在
  **Mac** 上跑（不破坏现有 GPT Action 的默认行为）；
- `thread_id → 哪台机器` 的映射会持久化：之后的 `continue / observe /
  steer / interrupt / read` 自动按映射路由到正确那台；映射缺失（如路由器
  重启过）时会安全地探测两端再决定；`/threads` 把两台机器的线程列表合并
  返回（Windows 离线时自动跳过，绝不影响 Mac 侧的正常请求）。

### 怎么开启（两步，都不动公开 URL）

1. **Mac（一次性，选不断链的窗口做）**：在仓库根放一个 gitignored 的
   `.bridge_worker_token`（内容随便一段长随机串），然后重启一次 bridge：
   `./scripts/stop_ngrok_bridge.sh && ./scripts/start_ngrok_bridge.sh`。
   启动脚本检测到该文件会自动开启 `BRIDGE_DUAL_HOST=true` + 注入
   `BRIDGE_WORKER_TOKEN`（等价于手动 export 这两个变量后重启）。公网 URL 与
   每个公开端点保持字节级不变；不配置时 dual-host 完全关闭，Mac 行为与以前
   一模一样。本 README 只说明做法，**不会替你执行这次重启**。
2. **Windows（第一次运行，全自动）**：在仓库根目录的 PowerShell 里执行
   （脚本会自动装 Python/Codex、还原 Desktop OpenAI 配置、DPAPI 存
   DeepSeek key，详见下节）：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\windows\start_local_codex_bridge.ps1
```

默认行为：本地 bridge 监听 `127.0.0.1:8321` 并验证 `/health` + `/ready`；
随后自动启动出站 worker `python -m bridge.worker`，长轮询 Mac 路由器
（默认 `https://diploma-ideology-skier.ngrok-free.dev`，可用
`-MacBridgeUrl` 或 `MAC_BRIDGE_URL` 覆盖），把 `start/continue/observe/
steer/interrupt/read/list` 任务转发到本机 8321 并回传结果。**不装 ngrok、
不占用任何公网域名**；worker token 首次以掩码输入（或 `-WorkerToken` 传参），
DPAPI 加密保存。Mac 路由器还没开 dual-host 时，worker 会带退避后台重试，
本地 bridge 照常可用——Windows 离线或路由器抖动都不会反过来拖慢 Mac。
停止：同一命令加 `-Stop`（只停本脚本启动的进程）。

本地纯调试 / 只起 bridge 不起 worker：加 `-NoNgrok`（该参数与旧版含义一致，
只是如今默认模式已经与 ngrok 无关，改名 `-LocalOnly` 更贴切，为兼容保留）。

### Desktop OpenAI / Bridge DeepSeek 隔离（Windows，用人话）

目标：**桌面上的 Codex 永远是 OpenAI，只有 Bridge 用 DeepSeek**，两者互不
污染，且不依赖任何 `--profile` 机制：

- **Desktop（OpenAI）**：普通 Codex 桌面应用 / 普通 `codex` 命令继续用
  `%USERPROFILE%\.codex`。若你之前为了跑 DeepSeek 改过这个目录，脚本会自动
  从官方备份 `%USERPROFILE%\.codex\backup-deepseek\config.toml` 还原 OpenAI
  配置；没有备份就写一个最小 OpenAI 配置（`gpt-5.6-sol`）。`state/`、
  `history/`、`auth.json` **从不读取、从不改动**。只有当确认用户级
  `OPENAI_BASE_URL` 指向 DeepSeek 时才清除它（普通代理/自建 base URL 保持
  原样，避免误删）。
- **Bridge（DeepSeek）**：app-server 永远使用独立 CODEX_HOME
  `%LOCALAPPDATA%\local-codex-bridge\codex-deepseek`；已有 DeepSeek 配置
  （`config.toml` + `providers/` 等）会自动迁移过去，缺失则自动生成
  DeepSeek provider 配置（`env_key` 引用，不内嵌 key）。
- **`codex-deepseek.cmd`**：临时设置上述专用 CODEX_HOME 后调用真实 `codex`
  的 wrapper（安装到 codex 同目录；仓库内也保留一份
  `scripts\windows\codex-deepseek.cmd`）。想从命令行走 DeepSeek 就用它，
  普通 `codex` 仍是 OpenAI。
- **DeepSeek key 不明文落盘**：首次掩码输入后，用 Windows DPAPI（当前用户）
  加密存 `%LOCALAPPDATA%\local-codex-bridge\secrets\deepseek.key.dpapi`；
  启动时只在子进程环境变量中注入，不打印、不入日志。worker token 同样
  DPAPI 保存。

### 旧 cutover 模式（legacy 可选）

早先 windows-bootstrap 的"Windows 独占固定 ngrok 域名、Mac 先停"模式仍可用
（加 `-NgrokCutover` 参数，可再叠 `-NoNgrok` 只做本地准备），但日常已被上面
的 dual-host 模式取代；两种模式二选一，不要同时开。其余默认值不变：工作根
`D:\work-of-jiaqi`（不存在自动创建）、codex 自动探测/安装（npm 用户前缀 →
官方 Windows 安装器，均免管理员）、实例状态
`%LOCALAPPDATA%\local-codex-bridge\local\`（对应 macOS
`${XDG_STATE_HOME:-$HOME/.local/state}/local-codex-bridge/<instance>/`，
Windows 面向 `local` 实例）。

### 安全与不变量

- 内部 worker API（`/internal/worker/poll|result|status`）使用**独立的**
  `BRIDGE_WORKER_TOKEN`（≠ GPT 用的 `.bridge_api_key`），端点 allowlist、
  队列/响应/超时全部有界，日志只含 job id/kind/状态码——prompt、key、token
  一律不入日志；worker 本地的转发目标由 kind 静态决定，路由器给什么 URL
  都改不了它。
- Mac 侧未配置 token 时内部 API 返回 404/503，公开 API 行为零变化；
  `/health` 仅在 dual-host 开启时附加 `dual_host` 状态字段。
- 除 secret 外前置条件全部自动：Python 3.8+ 缺失时 winget
  `--scope user` → python.org per-user 静默安装；Codex 缺失时自动安装；
  ngrok 只在 legacy cutover 模式需要（自动下载官方 zip 到
  `%LOCALAPPDATA%\ngrok`）。

## Mac → Windows cutover helper（windows-bootstrap，legacy 可选）

> **已被 dual-host 取代**：默认模式（见上节）里 Windows 不再需要 ngrok，
> 因此也不再需要搬运 ngrok authtoken。只有当你明确要回退到"单机独占固定
> 域名"的旧 cutover 时才需要本节内容。

把 Mac 的 bridge 凭据一次性搬到 Windows 的最小助手（小范围新增，不改 macOS
脚本）。只有两个迁移物，全部在 Mac 内存中打包加密、绝不打印到
stdout/stderr/日志：

- `.bridge_api_key`（仓库根；默认路径可用 `--bridge-key-file` 覆盖）；
- 当前 ngrok 用户 config（默认 `~/.config/ngrok/ngrok.yml`，其次
  `~/.ngrok2/ngrok.yml`，可用 `--ngrok-config` 覆盖）里的 authtoken——同账号
  即可接管固定域名（bridge 启动方式是 `ngrok http 8321 --url <固定域名>`，
  不需要迁移 tunnels 段）。可选读取 `.ngrok_domain` 只为提示域名是否与
  Windows 默认不同，域名本身不是 secret。

实现要点（Python stdlib + Windows PowerShell/.NET 内置 AES，无第三方依赖）：

- 随机一次性 AES-256 密钥：AES-256-CBC（随机 IV + PKCS#7）+ HMAC-SHA256
  认证（纯 Python FIPS-197 实现，带 KAT 测试）；
- Mac 只启动绑定局域网 IPv4 的临时 HTTP 服务：随机高端口、随机路径 token、
  最多存活 600 秒、只允许下载一次（下载后立即退出）、没有任何明文端点；
- 一次性传输密钥只在该命令内出现一次，服务器退出后即作废——复制到 Windows
  后不要保存、不要发给 AI/聊天工具；
- Windows 端单条 PowerShell 命令：下载 → HMAC 校验 → .NET AES-256 解密 →
  校验 key 为 64 hex 且 SHA256 与 Mac 相同（只比较 hash）→ 原子写入
  `D:\work-of-jiaqi\actions-bridge\.bridge_api_key`（去尾随换行；临时文件先
  `icacls` 收紧为当前用户再 rename）→ ngrok config 写入
  `%USERPROFILE%\.config\ngrok\ngrok.yml`（已存在则先备份
  `ngrok.yml.bak-<ts>`）→ `ngrok config check` 验证 → 删除全部临时
  明文/密文。全程不打印 bridge key 与 authtoken；
- 不自动停 Mac ngrok（保持控制链）：Windows 接管固定域名成功后再在 Mac 上
  `./scripts/stop_ngrok_bridge.sh`。

用法（Mac，仓库根；命令会打印 hash、一次性传输密钥与 Windows 端命令）：

```bash
python3 scripts/prepare_macos_cutover.py
```

把 `----- BEGIN PowerShell -----` 与 `----- END PowerShell -----` 之间的整段
粘贴到 Windows PowerShell（5.1+）执行。输出含：

```text
[cutover] key: 64 hex, SHA256 <64位hash> (matches Mac: True)
[cutover] ngrok config check: OK
```

然后按原 cutover 顺序：Windows 端运行
`powershell -ExecutionPolicy Bypass -File scripts\windows\start_local_codex_bridge.ps1
-NgrokCutover`（域名与 Mac 不同时加 `-Domain <域名>`）→ 成功后 Mac 端停
ngrok。若 Windows 仓库不在 `D:\work-of-jiaqi\actions-bridge`，重新生成时用
`--windows-key-path` 覆盖目标路径。测试：
`python3 -m pytest tests/test_prepare_macos_cutover.py -q`。

## 实例：local / hpc / maintenance（控制面隔离）

从 1.1.0 起，Bridge 进程在启动时被 `BRIDGE_INSTANCE`（默认 `local`）**钉扎**到唯一
实例；没有任务 API / helper 能切换运行中的实例。实例配置只含非 secret 字段
（`name` / `mode` / `approval_policy` / `network_access` / `codex_home` / `port` /
`runtime_dir` / `api_key_file` / `ngrok_domain_file`）；API key 与 ngrok 域名仍在
外部文件 / 环境变量中，配置里只存路径引用，从不复制 secret 内容。

| | `local`（默认） | `hpc` | `maintenance` |
|---|---|---|---|
| 用途 | 日常 coding / release 自动化（最小授权） | 独立远端运维（交互式 / 更宽范围，互不污染） | **Bridge 自身仓库维护**（显式 host-admin 维护窗口） |
| 沙箱模式 | `bridge-workspace`（纯 permission profile） | `workspace-write`（**模板永不使用 `danger-full-access`**） | `bridge-workspace`（**模板永不使用 `danger-full-access`**） |
| 审批策略 | `on-request`（自动化安全，无人值守） | `on-request` | `on-request` |
| 网络 | GitHub 域名白名单 | 开启 | GitHub 域名白名单 |
| CODEX_HOME | `~/.codex-deepseek`（独立） | `~/.codex-deepseek-hpc`（独立，互不共享） | `~/.codex-deepseek-maintenance`（独立，互不共享） |
| port / runtime | `8321` / `<state>/local/runtime` | `8322` / `<state>/hpc/runtime` | `8323` / `<state>/maintenance/runtime` |
| 公网域名 | `ngrok_domain_file`，或 legacy `.ngrok_domain` 回退（仅 local） | 必须显式配置 `ngrok_domain_file`；**不自动复用 local 域名** | 必须显式配置 `ngrok_domain_file`；**不自动复用 local 域名** |

规则：

- **并发隔离**：`local` + `hpc` + `maintenance` 同时运行仅当 port 与 runtime
  互不相同；任何碰撞（同 port 或同 runtime）都 fail-closed——start / stop /
  status / install / verify 全部拒绝，绝不静默覆盖。
- **无全局切换**：不存在"当前 profile"文件或切换接口；改实例配置是主机管理员
  的显式操作（先 stop，再 `scripts/bridge_instance.sh`，再 start）。
- **maintenance 不是普通 profile**：`maintenance` 是显式 host-admin 维护窗口，
  普通任务 API 没有切换入口。进入 / 离开窗口只能由主机管理员执行
  `scripts/activate_maintenance_instance.sh` / `scripts/deactivate_maintenance_instance.sh`
  （先停 local 再起 maintenance，或反向；不碰 hpc / 远端 jobs）。窗口内任务
  cwd 只允许 Bridge 仓库本身或其真实子目录（见「安全边界」）。
- **维护窗口可临时复用固定 endpoint**：为了让当前 Custom GPT Actions 在维护
  窗口仍走同一个固定 public endpoint，activate 脚本会**显式**把现有
  `.bridge_api_key` 与 `.ngrok_domain` 作为**路径引用**写入 maintenance 实例
  配置（只引用路径，绝不复制 / 打印 secret / 域名内容）；这是 host-admin
  显式行为，**不是** maintenance 默认模板行为——`create maintenance` 生成的
  配置两个引用均为空。
- **legacy 回退（deprecation）**：实例配置缺失时，start / status / stop 回退到
  旧单例行为（仓库根 `.runtime/`、`workspace-write`）并打印 warning；迁移后
  legacy 文件保持不动，直至你确认不再需要。
- **hpc / maintenance 无域名时**：启动给出明确报错（不暴露到 local 的域名端点）；
  local-only 模式不受影响。

实例状态写入只允许通过 `scripts/bridge_instance.sh`（list / show / create /
update / verify / migrate-current）；任务脚本、测试与迁移都是只读：

```bash
./scripts/bridge_instance.sh list
./scripts/bridge_instance.sh show hpc
./scripts/bridge_instance.sh create hpc --template hpc
./scripts/bridge_instance.sh create maintenance --template maintenance
./scripts/bridge_instance.sh update hpc port=8322
./scripts/bridge_instance.sh verify maintenance
./scripts/bridge_instance.sh migrate-current --dry-run   # 预览 legacy 单例 → local
./scripts/bridge_instance.sh migrate-current --apply     # 备份（600）后写入 local 实例
```

`update` / `migrate-current --apply` 先写同目录备份再落盘；`migrate-current`
只迁移非 secret 设置，不复制、不打印 `.bridge_api_key` / `.ngrok_domain` 内容。

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

# 指定开机自启的沙箱模式（默认 workspace-write；见「Bridge 沙箱模式与 git/GitHub 自动化」）
./scripts/install_launch_agent.sh --force --sandbox-mode bridge-workspace

# 按实例安装（1.1.0）：label 为 com.local.codex-bridge.<name>，plist 注入 BRIDGE_INSTANCE
./scripts/install_launch_agent.sh --instance local
./scripts/install_launch_agent.sh --instance hpc
./scripts/status_launch_agent.sh --instance hpc
./scripts/uninstall_launch_agent.sh --instance hpc --stop
```

说明：

- **模板在仓库、生成在本机**：plist 模板是 `scripts/launch_agent/com.local.codex-bridge.plist`，安装时把占位符 `__PROJECT_ROOT__` / `__HOME__` 替换为绝对路径后生成到 `~/Library/LaunchAgents/`，仓库里不落任何 secret 或本机路径。
- **launchd 的短 PATH**：plist 注入了常见 bin 目录（`~/.local/bin`、`/opt/homebrew/bin`、`/usr/local/bin` 等）；`start_ngrok_bridge.sh` 也会在 PATH 找不到时自动去这些目录找 `codex` / `ngrok`，其余工具均在 `/usr/bin`、`/bin` 内。
- **不重复启动**：`RunAtLoad` 登录只跑一次、`KeepAlive=false` 不自动重启；start 脚本本身幂等 + `.runtime` PID 校验 + 单实例锁（`.runtime/start.lock`），并发调用也只会有一个生效。
- **进程不被 launchd 回收**：plist 设置 `AbandonProcessGroup=true`，start 脚本退出后，后台的 bridge/ngrok 继续独立运行，`launchctl bootout` 卸载 agent 也不会误杀它们。
- **日志**：launchd 的 stdout/stderr 写到 `.runtime/launchagent.out.log` / `launchagent.err.log`（gitignored）。
- **沙箱模式注入**：`install_launch_agent.sh` 支持 `--sandbox-mode <mode>` 与 `--network-access true|false`，会写进生成的 plist 的 `EnvironmentVariables`，登录自启即用该模式（只适用于不带 `--instance` 的 legacy 安装）。
- **实例安装**：带 `--instance` 时 label 为 `com.local.codex-bridge.<name>`，plist 注入 `BRIDGE_INSTANCE=<name>`，port / runtime / CODEX_HOME / approval / network 全部从实例配置派生（拒绝再传 `--sandbox-mode` / `--network-access`）；不带 `--instance` 的安装保持 legacy `com.local.codex-bridge` 行为不变，迁移前可继续使用。
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

## Bridge 沙箱模式与 git/GitHub 自动化

Bridge 启动的 app-server 通过 `BRIDGE_SANDBOX_MODE` 选择沙箱边界（默认 `workspace-write`，即 V1 边界）：

| 模式 | 说明 | 网络 | 项目内 `git commit` |
|---|---|---|---|
| `workspace-write`（默认） | 工作区内读写/命令无审批，工作区外自动拒绝 | 默认关；`BRIDGE_NETWORK_ACCESS=true` 开启 | 否 |
| `bridge-workspace` | 纯 permission profile（`extends=":workspace"` + `.git/` 元数据写入 + GitHub 白名单网络，无全盘写权限）；要求专用 CODEX_HOME 不含 legacy `sandbox_mode` / `[sandbox_workspace_write]` | 开（仅 GitHub 域名白名单） | 是 |
| `danger-full-access` | 与直接 CLI 相同（无沙箱限制）；仅在你明确接受风险时选择 | 开 | 是 |

为什么直接跑 CLI 可以、走 Bridge 不行：直接 CLI 的 `~/.codex/config.toml` 是 `sandbox_mode="danger-full-access"`，而 Bridge 默认强制 `workspace-write`。在 workspace-write 下，Codex 0.147.0 把 `<workspace>/.git` 递归设为只读（官方文档 "Protected paths in writable roots"），所以 `git add` 写 `index.lock` 会被拒绝；同时默认 `network_access=false`，GitHub DNS/API/SSH 均不可达。

启用 `bridge-workspace`（推荐，最小权限；profile 由 Bridge 在启动时通过 `-c` 完整注入）。**前提**：专用 CODEX_HOME（`$CODEX_HOME`，默认 `~/.codex-deepseek`）的 `config.toml` 必须没有 legacy 沙箱键——官方规则是 beta permission profile 不能与旧 `sandbox_mode` / `[sandbox_workspace_write]` 混用：任何 loaded config 含它们，Codex 就退回旧 sandbox 并忽略 `default_permissions`。迁移见下文「专用 CODEX_HOME 迁移」。设置后**下一次**启动（普通 Terminal 运行 start，或下一次登录的 LaunchAgent）自动生效：

```bash
# 持久化模式（写入 gitignored 的 .bridge_sandbox_mode，下次启动生效）
./scripts/set_bridge_sandbox_mode.sh bridge-workspace

# 重启 bridge 使配置生效（stop/start 只动本脚本启动的进程）
./scripts/stop_ngrok_bridge.sh && BRIDGE_SANDBOX_MODE=bridge-workspace ./scripts/start_ngrok_bridge.sh

# 验证沙箱行为（真实 codex sandbox，不需要 API key；在普通 Terminal 运行）
./scripts/verify_bridge_git_automation.sh --network
```

LaunchAgent 方式（登录自启即用 `bridge-workspace`）：

```bash
./scripts/install_launch_agent.sh --force --sandbox-mode bridge-workspace
```

要点：

- `bridge-workspace` 的授权范围由 `http_server/server.py` 注入的 `-c` override 定义（与 `config/bridge-workspace.example.toml` 完全一致，测试保证同步）：profile `extends=":workspace"` 保留 baseline protections；workspace 根下 `"."` 可写、`.git/` 元数据可写、**`.git/hooks/` 显式只读**（放行 `.git/` 写入时 hooks 目录仍不可写；当前 Codex permission schema 只支持 read override、没有 deny 语义，按 schema 能力如实实现）、`.codex/` / `.agents/` / `.env` 保持只读；`":minimal"` 可读、`":tmpdir"` / `":slash_tmp"` 可写。网络只放行 GitHub 相关域名：`github.com`、`*.github.com`、`api.github.com`、`ssh.github.com`、`*.githubusercontent.com`、`objects.githubusercontent.com`、`raw.githubusercontent.com`。
- **绝不混用 legacy 沙箱键**：bridge-workspace 的 `-c` 注入里永远不会出现 `sandbox_mode` 或 `sandbox_workspace_write`（测试保证）；同时专用 CODEX_HOME config 若仍含这些键，Bridge **拒绝启动**（不是降级运行），并提示先跑迁移脚本。
- **0.147.0 协议要求：thread/start 显式选择 profile**：app-server 的命名
  permission profile 由 `thread/start` 参数 `config.default_permissions` 选择。
  bridge-workspace 下每个新 thread 都显式携带
  `config: {"default_permissions": "bridge-workspace"}`；thread/turn 参数绝不
  包含 legacy `sandbox` / `sandboxPolicy` 字段（枚举里没有 profile 值，出现即
  退回旧沙箱并禁用 profile，有测试逐模式断言）。legacy 两档不发送
  `default_permissions`。
- **不依赖用户 Git 代理**：bridge-workspace 下，Bridge 拉起的 app-server 子进程会用 `GIT_CONFIG_*` 环境变量把 `http.https://github.com.proxy` / `http.proxy` 覆盖为空（并丢弃 `http_proxy` 等环境变量），让 git 直连 GitHub，不依赖本机全局 HTTP 代理（如 Clash 等）；`~/.gitconfig` 本身不被修改。
- `BRIDGE_NETWORK_ACCESS=true` 只作用于 `workspace-write` 模式（等价官方 `[sandbox_workspace_write] network_access = true`）；开启后网络可达，但 `.git` 依旧只读，git 写操作仍失败。
- `danger-full-access` 与直接 CLI 行为一致：模型生成的命令可读写任意路径并访问网络。仅当你接受"ChatGPT 触发的会话拥有本机全权"时使用。

### 专用 CODEX_HOME 迁移

专用 CODEX_HOME（`$CODEX_HOME`，默认 `~/.codex-deepseek`）的 `config.toml` 必须为纯 permission-profile 状态。用仓库里的迁移脚本（只动专用 CODEX_HOME，先 dry-run / verify，再 apply）：

```bash
# 查看会改什么（只打印键名/表名，不打印任何值，尤其不打印 API key）
./scripts/migrate_codex_home_permissions.py --dry-run
./scripts/migrate_codex_home_permissions.py --verify   # 干净则 exit 0，否则 exit 1

# 迁移：移除 legacy `sandbox_mode` / `[sandbox_workspace_write]`，
# 写入 default_permissions = "bridge-workspace" + [permissions.bridge-workspace] 块
# （备份为同目录 config.toml.bak，chmod 600；其余配置与注释逐字节保留）
./scripts/migrate_codex_home_permissions.py --apply
```

脚本只处理上述三项：移除 legacy 沙箱键/表、设置 `default_permissions`、安装/更新 `[permissions.bridge-workspace]`（内容与 `config/bridge-workspace.example.toml` 一致）。model / model_provider / DeepSeek provider / env_key 名等其它配置和注释全部保留；输出永不包含被移除行的值或任何 secret。默认读取 `$CODEX_HOME/config.toml`（未设 `CODEX_HOME` 时用 `~/.codex-deepseek`），也可 `--config <path>` 指定。迁移后 `start_ngrok_bridge.sh` 在 bridge-workspace 模式下会自动 `--verify` 预检，未迁移就拒绝启动。

### 持久模式文件

- **持久模式文件**：`.bridge_sandbox_mode`（项目根目录，gitignored，`chmod 600`，无 secret）。生效优先级：`BRIDGE_SANDBOX_MODE` 环境变量 > `.bridge_sandbox_mode` 文件 > 默认 `workspace-write`。用 `./scripts/set_bridge_sandbox_mode.sh` 读写：

```bash
./scripts/set_bridge_sandbox_mode.sh               # 查看当前生效模式
./scripts/set_bridge_sandbox_mode.sh bridge-workspace  # 持久化（校验三档值）
./scripts/set_bridge_sandbox_mode.sh --unset       # 恢复默认
```

  切换后**下一次**启动生效：在普通 Terminal 执行 `./scripts/stop_ngrok_bridge.sh && ./scripts/start_ngrok_bridge.sh`；登录自启场景用 `launchctl kickstart gui/$(id -u)/com.local.codex-bridge` 重跑 LaunchAgent。不要从 Bridge 沙箱内部发起重启——沙箱内启动的任何进程都会继承受限的 seatbelt profile，新 Bridge 会起不来或无网络（见 SECURITY.md「持久模式文件」）。

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
- **沙箱与审批**：app-server 每次启动都显式传 `-c 'approval_policy="on-request"'`
  （默认值；`BRIDGE_APPROVAL_POLICY` 可显式 pin 为 `never`，`local` / `hpc`
  实例模板均为 `on-request`），不依赖本机 config；Bridge 不转发任何交互式审批
  请求给 ChatGPT。沙箱边界由 `BRIDGE_SANDBOX_MODE` 决定：默认 `workspace-write`
  （工作区内读写/命令无提示执行，工作区外自动拒绝，`.git` 只读、网络默认关）；
  `bridge-workspace` 额外放开项目 `.git` 元数据写入与 GitHub 白名单网络；
  `danger-full-access` 无沙箱限制（显式选择）。
- **无交互审批流**：Bridge 不转发任何 permission prompt 给 ChatGPT；需要权限提升的操作直接失败。
- **任务 cwd 守卫（1.1.0）**：`/start`（及带 `cwd` 的 `/continue`）接受任务工作区前，
  对真实路径做 canonicalize；`local` / `hpc` 拒绝 `$HOME` 及其祖先、本 Bridge
  仓库（相等 / 祖先 / 内部）、实例状态根、实例 CODEX_HOME（相等 / 祖先 / 内部），
  symlink 先解析无法绕过。错误为结构化 `TaskCwdError`（通用 reason + category，
  不泄露私有路径）；正常兄弟项目（如 `$HOME/Desktop/some-project`）不受影响。
  这样任务不能选 `$HOME` 或本 Bridge 仓库当"宽工作区"来改写控制面文件。
- **maintenance cwd 守卫（1.1.0）**：`maintenance` 是 Bridge 自维护窗口，守卫
  规则与 local/hpc 相反——只接受 Bridge 仓库根本身或真实子目录作为任务 cwd
  （realpath 后判定，symlink 不能逃逸）；`/`、`$HOME`、仓库祖先、仓库外项目、
  maintenance 实例状态根与 maintenance CODEX_HOME 一律拒绝。`BridgeCore.start`
  按实例 scope 选择校验器（`build_cwd_guard` 的 `scope`），`/continue` 依旧
  不接受 `cwd`，不会扩权。
- **控制面只读**：实例状态由 `scripts/bridge_instance.sh` 独占写入（目录 `700`、
  配置 `600`）；任务脚本 / 测试 / 迁移不写实例状态或 `.bridge_sandbox_mode`
  （回归扫描测试强制）。Bridge 维护（改实例、迁移、重启）是显式的主机管理
  工作流，不通过任务面放行。
- **PID 隔离**：start 脚本把 PID 记入 `.runtime/`，stop 只 kill 记录中的 PID；端口/域名被未托管进程占用时报错退出、绝不动它。
- **不入库清单**：`.bridge_api_key`、`.public_url`、`.ngrok_domain`、`openapi.ngrok.yaml`（含真实域名）、`.runtime/`、所有 `*.log`、`*.pid`、`__pycache__/` 均在 `.gitignore`。
- **本地优先**：HTTP 默认只绑 `127.0.0.1`，公网面只经过 ngrok 与 key 认证。

## 已知限制

- `approval_policy=on-request`（默认）下，工作区内的操作无需确认即可执行；任何需要交互式审批的操作仍会直接失败——Bridge 不转发审批请求，没有升级通道（`BRIDGE_APPROVAL_POLICY=never` 可进一步收紧为全部拒绝）。默认 `workspace-write` 下网络默认关、`.git` 只读；需要 Bridge 自己完成 git/gh 时启用 `bridge-workspace`（见「Bridge 沙箱模式与 git/GitHub 自动化」）。
- `steer` 在 Codex 0.147.0 中是排队语义：不打断当前生成，当前回复结束后才注入。需要立即转向请用 `interrupt` + `continue`。
- `observe` 单次最多 10s（GPT Actions 超时约束），长任务需要调用方循环等待。
- `assistant_text` 摘要截断（4000 字符）；`/threads` 单页 ≤20。
- 会记录 `Model metadata for deepseek-chat not found` warning（仅记录，不致命）。
- 运行时日志包含 thread 内容与命令文本，全部 gitignored，不要在日志里放秘密。
- 仅在 macOS arm64 + `codex 0.147.0` 实测；`windows-bootstrap` / `dual-host-router`
  分支新增的 Windows 支持（PowerShell bootstrap + 平台路径解析 + dual-host 路由器
  与出站 worker）只经过离线单测与平台条件测试，尚未在真实 Windows 主机实机
  验证；Linux 与其它 codex 版本未验证。
- 暂无 MCP layer；`bridge/` 的接口设计预留了未来在其上构建 MCP 的可能。

## 项目结构

```
bridge/                核心库
  client.py            CodexAppServerClient：spawn/shutdown app-server、JSON-RPC 收发、
                       通知分发、进程退出处理
  core.py              BridgeCore：start/continue/observe/steer/interrupt/list/read
  dual_host.py         dual-host 路由器核心：cwd 分类（Windows 盘符/UNC/WSL vs
                       macOS POSIX）、thread_id→host 持久映射、有界 worker 队列/
                       结果/心跳、远端操作带界等待（零依赖，任意平台可测）
  worker.py            Windows 出站 worker（python -m bridge.worker）：长轮询 Mac
                       路由器内部 API，allowlist 转发到本机 127.0.0.1:8321 并回传
  workspace_guard.py   任务 cwd 守卫：拒绝 HOME/Bridge 仓库/状态根/CODEX_HOME 作为任务工作区
                         （Windows 下按大小写不敏感 + 盘符根语义比较，macOS 行为不变）
  platform_paths.py    Windows 平台默认路径与 codex 探测：Desktop 保留
                       %USERPROFILE%\.codex（OpenAI），Bridge/CLI wrapper 用专用
                       %LOCALAPPDATA%\local-codex-bridge\codex-deepseek（DeepSeek）；
                       %APPDATA%\npm\codex.cmd / 原生 codex.exe、npm shim → node+entry
                       （纯函数，macOS 不使用）
http_server/           HTTP API（stdlib ThreadingHTTPServer，Bearer 认证）
  server.py            路由、参数校验、错误格式、openapi 校验点
scripts/
  bridge_instance.sh     实例状态唯一写入者：list/show/create/update/verify/migrate-current
  bridge_instance_lib.sh 实例只读共享库：状态根/配置解析/模式映射/碰撞检测（任务脚本只读）
  activate_maintenance_instance.sh   HOST-ADMIN：进入 maintenance 维护窗口（先停 local 再起 maintenance，
                             显式写入固定 endpoint 的路径引用；不复制/不打印 secret/域名内容）
  deactivate_maintenance_instance.sh HOST-ADMIN：离开维护窗口（停 maintenance、起 local；保留
                             maintenance 状态与 CODEX_HOME 供下次使用）
  start_ngrok_bridge.sh   后台启动 bridge + ngrok（幂等、PID 记录、health 检查）
  stop_ngrok_bridge.sh    只停止本脚本启动的进程
  install_runtime.sh         HOST-ADMIN：安装非 Desktop 稳定 runtime（staging + 原子 current 符号链接、
                             只含 allowlisted tracked 文件、无 secret .runtime-build-info、保留最近 2 个 release、
                             key/domain 路径引用迁移到 config root；--dest 供 temp-dir 测试）
  uninstall_runtime.sh       HOST-ADMIN：移除带托管 marker 的 runtime release（拒绝任意路径；默认保留
                             state/CODEX_HOME/config 凭据）
  run_local_supervisor.sh    前台 supervisor（仅 explicit local）：监控 bridge/ngrok PID，真退出退避补起；
                             pause marker（维护窗口）停 children 并等待、移除后恢复；哨兵移除或 TERM 时
                             安全停子进程并 exit 0（launchd 托管）
  supervisor_control.sh      本地 supervisor 控制：status/enable/disable/restart（哨兵 + kickstart/legacy 回退）
  install_launch_agent.sh    生成并加载 LaunchAgent（--instance local 装 per-instance supervisor 代理并迁移 legacy；
                             --force 重新加载；hpc/maintenance 拒绝自动托管）
  uninstall_launch_agent.sh 移除 LaunchAgent（--stop 可选同时停服务；local 同时移除哨兵）
  status_launch_agent.sh     查看自启与运行状态（agent/PID/健康/runtime source/release/supervisor）
  launch_agent/              LaunchAgent plist 模板（legacy + com.local.codex-bridge.local）
  migrate_codex_home_permissions.py 迁移专用 CODEX_HOME config：移除 legacy sandbox 键、安装 bridge-workspace profile（dry-run/verify/apply，备份 600，不打印 secret）
  verify_bridge_git_automation.sh 用真实 codex sandbox 验证 git commit 权限与只读 GitHub 连通性（-c 注入方式与 Bridge 一致）
  set_bridge_sandbox_mode.sh    持久化/查看/清除 .bridge_sandbox_mode（校验三档值，无 secret）
  bridge_mode_lib.sh            模式解析共享库（env > 文件 > 默认）
  pid_guard_lib.sh            PID 身份只读校验（stop 只杀本项目管理的进程）
  windows/
    start_local_codex_bridge.ps1  Windows bootstrap（默认 dual-host）：Desktop OpenAI /
                             Bridge DeepSeek 隔离与迁移、DPAPI secret 存储、起本地
                             bridge 127.0.0.1:8321 并验证 /health + /ready、自动起
                             出站 worker 连 Mac 路由器；-NoNgrok 只起本地、-NgrokCutover
                             回退旧单机 cutover、-Stop 只停本脚本进程（secret 不入日志）
    codex-deepseek.cmd    CLI wrapper：临时设 CODEX_HOME=专用 DeepSeek profile 后调用
                             真实 codex（普通 codex / Desktop 保持 OpenAI）
config/
  bridge-workspace.example.toml  bridge-workspace profile 参考模板（与 server.py 的 -c 注入规则一致，测试保证同步）
tests/
  test_instance_isolation.py  离线单测：实例隔离/策略/无切换/admin-only 写入/fallback/迁移/碰撞/运行时路径（41 项）
  test_workspace_guard.py     离线单测：cwd 守卫（HOME/仓库/状态根/CODEX_HOME/symlink）（19 项）
  test_maintenance_instance.py 离线单测：maintenance 模板/策略/域隔离/碰撞/cwd 守卫 scope/health 元数据/
                             activate-deactivate 脚本不变量 + supervisor 交接/回滚（52 项）
  test_runtime_supervisor.py 离线单测：runtime install/uninstall allowlist/原子 current/.runtime-build-info 无 secret/
                             --dest/根目录 700/marker 守卫、plist PathState、supervisor enable-disable/crash 补起/
                             pause-resume/TERM/哨兵、legacy 精确迁移、runtime-copy 运行、maintenance 协同（43 项）
  test_config_propagation.py  离线单测：spawn 参数包含 approval/sandbox override
  test_git_automation.py      离线单测：沙箱模式纯 profile 约束（不混用 legacy 键）、child env 代理清理、legacy 键检测、profile 模板授权范围；可选集成验证
  test_migrate_codex_home_permissions.py 离线单测：迁移脚本 dry-run/verify/apply、幂等、备份 600、不打印 secret
  test_bridge_core.py         集成测试：start/continue/observe/interrupt/进程退出（5 场景）
  test_bridge_actions.py      集成测试：7 个 action（含 steer 排队语义）
  test_http_api.py            集成测试：HTTP API + openapi 校验（12 个场景）
  test_dual_host.py           离线单测：dual-host 核心（cwd 分类/映射持久化/worker 队列
                             有界性/超时/auth/日志无 prompt）
  test_dual_host_http.py      离线单测：真实 HTTP handler + worker 循环（线程内 fake
                             Mac/Win app-server，无网络）：/start 按 cwd 路由、映射
                             路由、worker auth/offline/timeout、/threads 合并
  test_windows_support.py     离线单测：Windows 默认路径/codex 探测/npm shim/argv 兼容
                             （51 项：46 项任意平台 + 5 项 Windows-only）+ PowerShell
                             bootstrap 结构/DPAPI/worker 模式/secret 面静态检查
.github/workflows/ci.yml   GitHub Actions：离线安全测试 + py_compile + bash -n + plist lint（无 secret/网络）
openapi.yaml            公共 Actions 模板（servers URL 为占位符）
schemas/                codex app-server 协议 JSON Schema（v1/v2，参考用）
CONTRIBUTING.md         贡献指南：本地测试、代码风格、secret 规则、PR checklist
docs/
  instance-isolation.md  实例隔离架构：状态布局、local/hpc/maintenance 示例、迁移、并发规则、回滚
                          以及运行时自动恢复（runtime/supervisor/launchd、恢复矩阵、维护窗口协同）
  chatgpt-codex-operating-policy.md  ChatGPT↔Codex 操作协议（Custom GPT Instructions 的仓库版本化副本，ChatGPT 主导 reasoning/review、Codex 本机执行）
  unattended-automation.md  本机无人值守接管框架：LaunchAgent + monitor 周期检查、终态邮件通知、rearm 与安全边界（通用设计，不含项目私有配置）
  zhihu-v1.md            知乎长文（已发布口径）
  xhs-v1.md              小红书发布稿（逐卡文案 + 截图纪律，无虚构图）
  content-publishing-checklist.md  知乎/小红书发布前检查清单（配图、隐私、时点同步）
  release-process.md     v1.0.x 发布 checklist 与 tag 纪律
  v1-development-notes.md  脱敏工程历史与已知限制
```

## 运行时部署与自动恢复（1.1.0）

launchd 自动恢复从 1.1.0 起不再依赖 Desktop 仓库：稳定 runtime 装在
`${XDG_DATA_HOME:-$HOME/.local/share}/local-codex-bridge/`（避开 Desktop TCC），
state 仍在 `${XDG_STATE_HOME:-$HOME/.local/state}/local-codex-bridge/<instance>/`，
稳定凭据/路径引用放 `${XDG_CONFIG_HOME:-$HOME/.config}/local-codex-bridge/`
（目录 `700`、文件 `600`，内容绝不打印，repo 原文件不删）。

一次性 host-admin 部署（需普通 Terminal，`~/.local/share` 不在任务沙箱写权限内）：

```bash
./scripts/install_runtime.sh --instance local     # runtime + 凭据路径引用迁移
./scripts/install_launch_agent.sh --instance local  # per-instance 代理 + 哨兵 + legacy 迁移
./scripts/status_launch_agent.sh --instance local   # 复核
```

恢复矩阵：

- ngrok 网络闪断：ngrok 客户端自行重连；supervisor 只按 PID 身份判断，网络/健康暂时失败**不杀**进程。
- bridge/ngrok 真退出：前台 supervisor（`run_local_supervisor.sh --instance local`）以退避/节流补起。
- supervisor 崩溃 / logout / reboot：launchd（`com.local.codex-bridge.local`，RunAtLoad +
  KeepAlive `PathState` 绑定实例状态 runtime 下绝对路径 `supervisor.enabled` 哨兵，
  ThrottleInterval=10）拉回；supervisor/launchd 日志在 local instance state runtime。
- maintenance 窗口：activate 在停 local 前写 `activate.marker`（instance state）并创建
  `<state>/local/pause.marker`（哨兵保持，前台 supervisor 存活、停住 local children，launchd 无
  crash-loop），再停 local/起 maintenance；成功进入窗口 marker 保留；失败 rollback 先清 pause marker
  再恢复原 enabled/disabled 状态与 local；deactivate 停 maintenance 后清 pause marker，优先让
  launchd supervisor 恢复（已在运行则自行恢复，否则哨兵 + kickstart），agent 未装/不可用时 fallback
  到 legacy local start，并验证 local/public health；旧窗口（无 marker）按 legacy 处理。
- 升级：`install_runtime.sh` 保留最近 2 个 release，`current` 原子切换；旧 release 用严格路径 +
  托管 marker 守卫清理。

安全/边界：install/uninstall 只接受 `--instance local`；supervisor 只托管 local，hpc/maintenance
按需手动起停、永不自动托管；全程无 `pkill`/`killall`，`rm -rf` 仅限 release 模式匹配的受守卫路径；
plist 不含 secret/domain。**2026-08-14 已在真实主机普通 Terminal 完成完整
bootstrap 实机验证**（runtime 安装、LaunchAgent 装载、supervisor 实机运行、
真实 bridge crash-recovery 新 PID、maintenance→local→maintenance round-trip，
见 `docs/release-validation-v1.1.0.md` 第 3 节）。

## V1 状态

- **当前版本 = `1.1.0`（已发布，2026-08-14）**：`bridge.__version__`、
  `openapi.yaml`、app-server initialize clientInfo 统一为 `1.1.0`。范围见
  CHANGELOG `[1.1.0] - 2026-08-14`：实例钉扎控制面隔离
  （`local` / `hpc` / `maintenance`）、任务 cwd 守卫、多实例 LaunchAgent、
  `migrate-current`、maintenance 维护窗口、single-writer host-ops lock 与
  运行时自动恢复（runtime + supervisor + per-instance LaunchAgent），
  以及原 1.0.1 unreleased 内容（sandbox 模式、`bridge-workspace` profile、
  CODEX_HOME 迁移 helper、PID 守卫、CI / CONTRIBUTING / docs）。**1.0.1 从未
  发布**：本 README / CHANGELOG 不再声称 1.0.1 存在。
- 已发布历史：GitHub Release `v1.0.0`（`Xin-Jiaqi/local-codex-bridge`，public，
  BSD-3-Clause；tag `v1.0.0` 指向 `fa82e91`，**不移动**）。v1.0.0 发布版只有
  `workspace-write`（V1 边界）一个沙箱模式，没有实例 / cwd 守卫 / 多实例
  LaunchAgent。
- 离线单测（当前可复现，无需 app-server，共 **343 项** = windows-bootstrap 分支
  290 项 + 本次新增 53 项：dual-host 26 项 + dual-host-http 12 项 + windows_support
  由 36 增至 51 项；macOS 本地跳过 9 项）：
  `tests/test_config_propagation.py`（3）、`tests/test_sandbox_mode.py`（7）、
  `tests/test_instance_isolation.py`（41）、`tests/test_workspace_guard.py`（19）、
  `tests/test_maintenance_instance.py`（52）、`tests/test_runtime_supervisor.py`（43）、
  `tests/test_git_automation.py`（29：28 离线 + 1 可选 sandbox 集成）、
  `tests/test_migrate_codex_home_permissions.py`（17）、`tests/test_pid_guard.py`
  （10：7 项纯匹配 + 3 项 live-process 跳过）、
  `tests/test_activate_runtime_autorecovery.py`（11）、
  `tests/test_bootstrap_autorecovery_command.py`（11）、
  `tests/test_host_ops_lock.py`（11，single-writer host-ops lock）、
  `tests/test_windows_support.py`（51：46 项任意平台 + 5 项 Windows-only 平台条件测试）、
  `tests/test_dual_host.py`（26：cwd 分类/映射持久化/队列有界/超时/worker 转发桩/
  auth/日志无 prompt）、`tests/test_dual_host_http.py`（12：真实 HTTP handler +
  worker 循环，线程内 fake Mac/Win bridge，无网络：/start cwd 路由、映射路由、
  worker auth/offline/timeout、/threads 合并）。
  跳过 9 项：pid guard 3 项 live-process（无 `ps` 的沙箱内跳过）+ git automation
  1 项可选 sandbox 集成（需普通 Terminal + `RUN_SANDBOX_TESTS=1`）+ windows_support
  5 项 Windows-only（`os.name == "nt"` 才执行）。CI 跑同一集合（集成验证除外）。
- 集成测试需要真实 app-server + DeepSeek key，无 CI 自动化、未在发布后复跑：`python3 tests/test_bridge_core.py`、`python3 tests/test_bridge_actions.py`、`python3 tests/test_http_api.py`。最近一次完整实测（2026-08-11）为全 PASS：core 5/5、actions 7/7、HTTP API 11/11、公网 tunnel 6/6（历史记录）。当前 `test_http_api.py` 含 12 个唯一场景（含 openapi 校验，部署副本缺失/占位按通过处理），与历史记录的差异未复跑确认。
- 已实测能力：start/continue/observe/steer/interrupt/list/read、本地读写、shell、native thread 连续工作、Bearer API Key、workspace-write + approval_policy=on-request、Bridge/ngrok 后台启动、PID 管理、stop 隔离、health checks、完整 stop→start→health 生命周期；runtime install/uninstall、supervisor enable/disable、crash 补起、pause-resume 与 maintenance 交接在 temp-dir 离线测试验证；2026-08-14 真实 host round-trip 已实机验证 runtime 安装、LaunchAgent 装载、supervisor 实机运行与真实 bridge crash-recovery（见 `docs/release-validation-v1.1.0.md` 第 3 节）。
