# V1 工程笔记（Engineering Notes）

> 面向维护者的工程历史与决策记录，不是 debug log。
> 脱敏声明：本文件不含真实 ngrok 域名、API key/token、个人绝对路径、PID、
> shell history 或本机配置内容；路径一律写 `$HOME`/`~` 或相对路径。

## 1. 背景与定位

项目把 ChatGPT（Custom GPT Actions）接到本机 Codex CLI：ChatGPT 通过 HTTPS
调用本地 Bridge，Bridge 持有一个常驻 `codex app-server`（stdio JSON-RPC）会话，
让远程对话能读写工作区、跑 shell、连续完成多轮任务。定位是**个人单机工具**：
不承诺多用户、审批流、审计。

技术栈刻意保持零第三方运行时依赖：Python 标准库 HTTP server + 手写 JSON-RPC
客户端；macOS 上由 bash 脚本管理后台进程与 launchd 自启。

## 2. 架构决策记录

### 2.1 app-server + native thread
- Bridge 只持有**一个** app-server 进程；每个 ChatGPT 会话对应一个 native
  thread（`/start` 创建，`/continue` 续跑）。thread 状态在 server 内存中，
  进程重启即丢失——这是 V1 接受的取舍（单机、短任务）。
- 初始化握手用 `clientInfo {name: local-codex-bridge-core, version}`，版本号与
  `openapi.yaml`、`bridge.__version__` 三处统一，防止协议漂移。

### 2.2 7 个 action
`health / start / continue / observe / steer / interrupt / list / read`：
- `observe` 单次最多 10s（GPT Actions 超时约束），长任务需要调用方循环等待。
- `steer` 在 Codex 0.147.0 是**排队语义**：不打断当前生成，当前回复结束后才注入。
- `read` 用于把文件内容取回对话，缓解模型侧上下文缺失。
- OpenAPI 全部标 `x-openai-isConsequential: false`（调用前不弹确认）——这只是
  对 ChatGPT 的声明，真正的安全边界是 key 认证 + 工作区沙箱，见 SECURITY.md。

### 2.3 tunnel 与 auth
- 公网面用 ngrok 固定域名（域名存 gitignored 的 `.ngrok_domain`，不随仓库发布）。
- 除 `/health` 外所有端点要求 `Authorization: Bearer <BRIDGE_API_KEY>`
  （`hmac.compare_digest` 常量时间比较；key 只存在于环境变量）。
- `/health` 无认证是有意设计（隧道/探测用），不返回敏感信息。

### 2.4 approval 与 V1 边界
- app-server 固定 `approval_policy="never"` + 沙箱边界启动：不转发
  `requestApproval` 给 ChatGPT，工作区外一律自动 denied。
- 这是"无人值守远程操作"与"最小权限"之间的 V1 平衡：宁可拒绝，不给升级通道。

### 2.5 lifecycle / launchd
- bash 脚本管理：`start/stop/status`，`.runtime/` 下 PID 与锁，stop 只停自己
  启动的进程；launchd LaunchAgent 登录自启，`AbandonProcessGroup` 防止回收。
- 实测教训：**不要从 Bridge 沙箱内部发起重启**——沙箱内任何进程（含
  `launchctl kickstart` 拉起的 job）都会继承 seatbelt profile（exit 126 /
  无网络），因此切换沙箱模式必须在普通 Terminal 做 stop/start。

## 3. 权限修复：bridge-workspace permission profile（1.1.0 线）

背景：V1 的 `workspace-write` 下，Codex 0.147.0 把工作区根 `.git` 设为 protected
path（只读），且默认无网络，导致 Bridge 无法自己 `git add/commit` 或访问 GitHub。

### 3.1 第一版尝试（已废弃的假设）
先做 `BRIDGE_SANDBOX_MODE` 三档开关，其中 `bridge-workspace` 通过 `-c` 注入
`default_permissions` + profile 表，并假设 `-c` 层可以压过配置文件里的
`sandbox_mode`。此假设**作废**：官方规则是 beta permission profile 不能与
legacy `sandbox_mode` / `[sandbox_workspace_write]` 混用——任何 loaded config
含它们，Codex 就退回旧 sandbox 并忽略 `default_permissions`。

### 3.2 修正后的实现（1.1.0 candidate）
- `bridge-workspace` 是**纯 profile**：`-c` 注入只有 `default_permissions` +
  `[permissions.bridge-workspace]`（`extends=":workspace"` 保留 baseline
  protections；`:workspace_roots` 下 `.git/` 可写，`.codex/`/`.agents/`/`.env`
  只读；network `enabled=true` + GitHub 域名 allowlist）。**永不注入**
  `sandbox_mode` / `sandbox_workspace_write`（有测试强制）。
- 启动守卫：bridge-workspace 下若专用 CODEX_HOME（`config.toml`、`config/*.toml`、
  项目 `.codex/config.toml`）仍有 legacy 键，server 拒绝启动（exit 2），提示先迁移。
- 迁移 helper：`scripts/migrate_codex_home_permissions.py`
  （dry-run/verify/apply）只移除 legacy 键/表、写入 profile、保留其它配置，
  输出永不打印值，apply 前写同目录 600 权限备份。
- Git 代理隔离：bridge-workspace 子进程环境用 `GIT_CONFIG_*` 覆盖
  `http.https://github.com.proxy`/`http.proxy` 为空并丢弃代理环境变量，
  让 git 直连 GitHub，不依赖本机全局代理；`~/.gitconfig` 不动。
- `workspace-write`（默认）与 `danger-full-access` 保持 legacy 机制不变；
  `danger-full-access` 仅显式选择。
- 0.147.0 协议级要求（实测补丁）：命名 profile 除了 `-c` 注入，还要在
  `thread/start` 参数中显式选择——`config: {"default_permissions":
  "bridge-workspace"}`；thread/turn 参数绝不能带 legacy `sandbox` /
  `sandboxPolicy`（枚举无 profile 值，出现即退回旧沙箱并禁用 profile）。
  已按 `schemas/v2/ThreadStartParams.json` / `TurnStartParams.json` 对三档模式
  做参数级回归测试（`tests/test_git_automation.py`）。

### 3.3 为什么是"最小授权"而不是 danger-full-access
直接 CLI 用 `danger-full-access`（无沙箱）日常可用，但 Bridge 是公网可触达的
远程操作面，不能给 ChatGPT 全盘权限。`bridge-workspace` 只放行项目 `.git/`
写入与 GitHub 主机，是"能完成 git/GitHub 自动化"的最小集合。风险仍在：
git 是"可执行内容"，合入前必须 review（`git diff` 后再合）。

### 3.4 第二轮审查加固（1.1.0 candidate）
- PID 身份守卫：早期 stop/start 按 `.runtime/*.pid` 里的数字 PID 操作。PID 可被
  系统回收复用，裸数字不够。现在统一先 `ps -p <pid> -o command=` 只读校验
  命令形态（Bridge：`python3 -m http_server` + 项目 `.runtime` 上下文；ngrok：
  `ngrok http 8321`），校验通过才 SIGTERM/SIGKILL 或复用；校验失败只报告
  stale/unmanaged 并清理 pid 文件，不 kill；全程不用 pkill/killall
  （`scripts/pid_guard_lib.sh`，start/stop 共享）。
- preflight 对齐：start 预检与 server guard 的扫描范围一致，都覆盖
  `CODEX_HOME/config.toml`、`CODEX_HOME/config/*.toml` 与项目 `.codex/config.toml`
  （迁移脚本 `--project-root` 只读扫描，apply 永不改项目配置），避免
  "start 预检通过、server 再拒绝"的假通过。

## 4. 实例隔离：控制面移出任务工作区（1.1.0）

背景：v1.0.0 只有"当前配置"（legacy 单例），任务工作区没有边界限制——任务一旦
选 `$HOME` 或本仓库当 cwd，就有机会改写 `.bridge_sandbox_mode` / `.runtime/` /
仓库脚本。第一版尝试做了仓库内 `.bridge-control/active_profile` 可切换 profile，
安全审查判为 P0（任务侧存在"切到更宽 profile"的切换面）后整体废弃，不保留任何
可切换 active_profile 机制。

### 4.1 最终设计：实例钉扎
- 控制面状态移到任务工作区之外：
  `${XDG_STATE_HOME:-$HOME/.local/state}/local-codex-bridge/<instance>/`
  （目录 `700`、配置 `600`）；仓库内没有任何"当前 profile"指针。
- `BRIDGE_INSTANCE`（默认 `local`）在进程启动时钉扎；任务 API / helper 无切换
  接口。实例配置非 secret（`name` / `mode` / `approval_policy` / `network_access`
  / `codex_home` / `port` / `runtime_dir` / `api_key_file` / `ngrok_domain_file`）；
  secret 留在外部文件 / env，配置只存路径引用。
- 唯一写入者 `scripts/bridge_instance.sh`（list / show / create / update /
  verify / migrate-current）；`scripts/bridge_instance_lib.sh` 只读；回归扫描
  测试强制任务侧永不写实例状态或 `.bridge_sandbox_mode`。

### 4.2 任务 cwd 守卫
- `bridge/workspace_guard.py`：`/start`（及带 `cwd` 的 `/continue`）接受前先
  canonicalize 真实路径（symlink 先解析）；拒绝 `$HOME` 祖先链、Bridge 仓库
  （相等 / 祖先 / 内部）、实例状态根、实例 CODEX_HOME（相等 / 祖先 / 内部）。
- 结构化 `TaskCwdError`（通用 reason + category，不泄露私有路径）；正常兄弟
  项目不受影响。

### 4.3 策略与并发
- `local` = `bridge-workspace` 最小授权；`hpc` = `workspace-write` + `on-request`
  + 网络开启，独立 CODEX_HOME / port / runtime，**永不** `danger-full-access`，
  不自动复用 local 公网域名（无 hpc 域名时启动报错，不做 local 域回退）。
- 并发仅当 port 与 runtime 互不相同；任何碰撞 fail-closed（start / stop /
  status / install / verify 全部拒绝）。
- 迁移：`migrate-current --dry-run / --apply` + `verify NAME`；legacy 文件不动；
  缺实例配置时回退 legacy 单例 + warning（deprecation 路径）。
- `.git/hooks/` 在 bridge-workspace 中显式 `read`（当前 Codex permission schema
  只支持 read override、无 deny 语义——如实实现，不虚构 deny 规则）。

## 5. 已知限制（截至 1.1.0 candidate）

- 无 MCP layer；`bridge/` 接口为后续扩展留位。
- 无审批流/审计/多用户隔离；不适合需要这些能力的场景。
- thread 状态进程内保存，Bridge 重启即丢；长任务依赖调用方循环 `observe`。
- 仅实测 macOS arm64 + codex 0.147.0；平台/版本迁移未验证。
- profile 网络 allowlist 不是"只能访问 GitHub"的强隔离（DNS 重绑定等攻击面
  与官方 `network_proxy` 语义一致）。
- 0.147.0 的 steer 排队、protected `.git`、profile 行为均未文档化或属于 beta，
  升级 Codex 前需重新验证（`verify_bridge_git_automation.sh --network`）。
- 测试口径分层：离线单测/CI 为当前可复现（1.1.0 共 153 项，含 maintenance 28 项）；2026-08-11 的
  core/actions/HTTP/tunnel 全 PASS 为历史手动实测，未在发布前复跑（见 README
  「V1 状态」）。
- 实例隔离只保证任务 cwd 边界与实例钉扎；仍无多租户、审计与审批流。

## 6. 里程碑

- 2026-08-11：core/actions/HTTP/tunnel 完整手动实测全 PASS（历史记录）。
- 2026-08-12：v1.0.0 发布（tag `v1.0.0`）；同日完成沙箱模式开关、持久模式文件、
  bridge-workspace 纯 profile 修正与迁移 helper——全部归档进 1.1.0 unreleased
  候选（1.0.1 从未发布）。
- 2026-08-13：实例钉扎控制面（`local` / `hpc`）、任务 cwd 守卫、多实例
  LaunchAgent、`migrate-current` / `verify` 完成；离线单测 125 项。
- 2026-08-13：新增第三个固定实例 `maintenance`（Bridge 自维护窗口）、
  `validate_maintenance_cwd` scope 守卫、health 元数据、activate/deactivate
  host 脚本；离线单测增至 153 项；架构归档见 `docs/instance-isolation.md`。
