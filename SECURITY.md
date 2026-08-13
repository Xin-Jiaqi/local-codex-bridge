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
- app-server 显式以 `approval_policy="on-request"` 启动（`-c` 显式传入，不依赖本机 config；
  `BRIDGE_APPROVAL_POLICY` 可 pin 为 `never`；`local` / `hpc` / `maintenance` 实例模板均为
  `on-request`），
  沙箱边界由 `BRIDGE_SANDBOX_MODE` 选择（默认 `workspace-write`）：
  - 允许：workspace 内文件读写、命令执行，无需人工确认；
  - 拒绝：workspace 外的一切操作（自动 denied），无升级通道；
  - bridge 不转发任何 `requestApproval` 类请求给 ChatGPT。
- **持有 Bearer key ≈ 获得 workspace 内的命令执行权**。公网暴露时请严格控制 key 分发，
  并定期轮换。
- `x-openai-isConsequential: false` 只是向 ChatGPT 的声明（调用前不弹确认），
  不是安全控制；真正的边界是 key 认证 + 工作区沙箱 + 任务 cwd 守卫。
- **跨会话配置改写威胁（1.1.0）**：拿到 key 的会话只应影响"任务面"。控制面
  （实例配置、状态根、本 Bridge 仓库、CODEX_HOME）不允许被任务当作工作区：
  任务 cwd 守卫在 `/start` / `/continue` 接受路径时拒绝 `$HOME`、Bridge 仓库、
  实例状态根、实例 CODEX_HOME（相等/祖先/内部，symlink 先解析）；运行中的实例
  也无法被任何任务 API 切换（`BRIDGE_INSTANCE` 启动时钉扎）。

## 沙箱模式（BRIDGE_SANDBOX_MODE）

> 版本说明：本节描述的是 v1.1.0（unreleased minor candidate）行为。v1.0.0
> 已发布版只有 `workspace-write` 一种沙箱边界——没有三档 `BRIDGE_SANDBOX_MODE`
> 开关、没有 `bridge-workspace` permission profile、没有 CODEX_HOME 迁移脚本、
> 没有实例隔离与 cwd 守卫。1.0.1 从未发布。

| 模式 | 权限边界 | 何时使用 |
|---|---|---|
| `workspace-write`（默认） | 工作区内读写/命令；`.git` 为 protected path（只读）；网络默认关，`BRIDGE_NETWORK_ACCESS=true` 可开（官方 `[sandbox_workspace_write] network_access = true`） | 默认日常使用；不需要 Bridge 自己 commit/push |
| `bridge-workspace` | 纯 beta permission profile（`extends=":workspace"` 保留 baseline protections）：workspace 边界与 workspace-write 相同，额外放开 workspace 根下 `.git/` 元数据写入，**`.git/hooks/` 显式只读**（schema 只支持 read override，无 deny 语义，按能力如实实现）；`.codex/` / `.agents/` / `.env` 保持只读；网络仅放行 GitHub 域名白名单。**绝不与 legacy `sandbox_mode` / `[sandbox_workspace_write]` 混用**：任何 loaded config 含它们，Codex 会退回旧 sandbox 并忽略 `default_permissions`，因此专用 CODEX_HOME 必须迁移干净（`scripts/migrate_codex_home_permissions.py`），server 在 bridge-workspace 下发现 legacy 键会**拒绝启动**。profile 由 Bridge 启动时经 `-c` 完整注入（`http_server/server.py`），`config/bridge-workspace.example.toml` 为等价参考模板 | 需要 Bridge 在项目内自己完成 git commit/tag 以及 GitHub 只读/推送操作的最小授权方案 |
| `danger-full-access` | 无沙箱限制：模型生成的命令可读写任意路径、访问任意网络 | 与直接 CLI（`~/.codex/config.toml` 的 `danger-full-access`）行为对齐的完全自动化；**风险自担** |

风险说明：

- `bridge-workspace` 让 Bridge 可写项目 `.git`（index/objects/refs）。git 是"可执行内容"：
  一旦提交被合入，模型生成的文件后续可能在你的机器上以正常权限被执行。请保持常规
  review 习惯（`git diff` 后再合入），不要让未经审阅的 commit 自动 push。
- `bridge-workspace` 的网络白名单覆盖 GitHub 所需主机：`github.com`、
  `*.github.com`、`api.github.com`、`ssh.github.com`、`*.githubusercontent.com`、
  `objects.githubusercontent.com`、`raw.githubusercontent.com`（HTTPS API、git
  over HTTPS/SSH、objects/codeload 下载）。它不等于"只能访问 GitHub"的强隔离；
  DNS 重绑定等边界攻击面与 Codex 官方 `network_proxy` 语义一致。
- **不依赖用户 Git 全局代理**：bridge-workspace 下 Bridge 拉起的 app-server 子进程
  会把 `http.https://github.com.proxy` / `http.proxy` 经 `GIT_CONFIG_*` 环境变量覆盖
  为空并丢弃 `http_proxy` 等环境变量，git 直连 GitHub（本机全局 HTTP 代理在沙箱内不可达，不能作为依赖）；`~/.gitconfig` 不被修改，仅限 Bridge
  子进程环境。
- **线程级 profile 选择（Codex 0.147.0 协议要求）**：命名 permission profile 由
  `thread/start` 参数 `config.default_permissions` 显式选择。bridge-workspace 下
  Bridge 对每个新 thread 都发送 `config: {"default_permissions":
  "bridge-workspace"}`，且 thread/turn 参数绝不包含 legacy `sandbox` /
  `sandboxPolicy` 字段（其枚举无 profile 值，出现即让该线程退回旧沙箱并忽略
  profile，测试强制）；legacy 两档绝不携带 `default_permissions`。
- **专用 CODEX_HOME 不得混用 legacy sandbox keys**：官方规则是 beta permission
  profile 不能与旧 `sandbox_mode` / `[sandbox_workspace_write]` 共存——任何 loaded
  config 含它们即禁用 profile。迁移脚本 `scripts/migrate_codex_home_permissions.py`
  只做三件事：移除 legacy `sandbox_mode` 键与 `[sandbox_workspace_write]` 表、写入
  `default_permissions = "bridge-workspace"`、安装/更新 `[permissions.bridge-workspace]`
  块；其它配置与注释逐字节保留，输出不打印任何值（尤其不打印 API key），apply 前
  写同目录 `config.toml.bak`（chmod 600）。支持 `--dry-run` / `--verify` / `--apply`。
  迁移后 `start_ngrok_bridge.sh` 在 bridge-workspace 模式自动 `--verify` 预检。
- `danger-full-access` 与直接 CLI 相同：ChatGPT 触发的会话可以读写你的整个用户目录、
  ssh keys、token 文件等。仅在你明确接受该风险时使用；公网暴露时尤其危险。
- `.git` 在 `workspace-write` 下为只读是 Codex 0.147.0 的设计（protected paths），
  不是 bridge bug；要获得 git 写入需切换到 `bridge-workspace` 或 `danger-full-access`。

## 部署建议

- 隧道域名是个人 ngrok 账号资产，存于 `.ngrok_domain`（gitignored），不要提交。
- 当前版本无内置限流、无失败锁定、无 IP 白名单；长期公网使用建议在隧道/网关层补充。
- 运行时日志包含 thread 内容与命令文本，不要在日志中写入秘密。
- `/health` 无认证是有意设计（供隧道与探测），不返回敏感信息。

## 持久模式文件

- `.bridge_sandbox_mode`（项目根目录，gitignored，`chmod 600`）只存三档模式之一，
  不含任何 secret；`scripts/set_bridge_sandbox_mode.sh` 校验后写入/读取/清除。
  生效优先级：`BRIDGE_SANDBOX_MODE` 环境变量 > 文件 > 默认 `workspace-write`。
- 不要从 Bridge 沙箱内部发起重启（2026-08-12 实测）：沙箱内发起的任何进程都会
  继承发起方的 seatbelt profile——`launchctl kickstart` 拉起的 launchd job 同样
  继承（实测 exit 126 / Operation not permitted），直接 start 的新 Bridge 则可能
  无网络（无法访问 api.deepseek.com）。切换模式请在普通 Terminal 执行
  `stop_ngrok_bridge.sh` + `start_ngrok_bridge.sh`，或重跑 LaunchAgent。

## 实例与控制面隔离（1.1.0）

- **控制面状态在任务工作区之外**：实例配置与运行状态位于
  `${XDG_STATE_HOME:-$HOME/.local/state}/local-codex-bridge/<instance>/`
  （目录 `700`、配置 `600`）。仓库内没有 `.bridge-control/active_profile` 之类
  的可切换 profile 指针——任务（即使拿到 Bearer key）没有"切到更宽实例"的接口。
- **实例钉扎**：Bridge 进程启动时由 `BRIDGE_INSTANCE`（默认 `local`）钉扎；
  运行中不可切换。实例配置只含非 secret 字段；`api_key_file` /
  `ngrok_domain_file` 只存路径引用，secret 内容仍留在外部文件 / env。
- **admin-only 写入**：实例状态只由 `scripts/bridge_instance.sh` 写入（create /
  update / migrate-current 先备份，600 / 700）；`bridge_instance_lib.sh` 与任务
  脚本、测试、迁移都是只读——回归扫描测试强制"任务侧永不写实例状态或
  `.bridge_sandbox_mode`"。Bridge 维护（改实例、迁移、重启）是显式的主机管理
  工作流，不通过任务面放行。
- **hpc 策略**：hpc 模板固定 `workspace-write` + `on-request` + 网络开启，
  永不使用 `danger-full-access`（admin 脚本与测试强制）；独立 CODEX_HOME /
  port / runtime，不自动复用 local 的 ngrok 域名——hpc 未配置
  `ngrok_domain_file` 时启动报错（不做 local 域回退），local-only 模式不受影响。
- **maintenance 策略**：`maintenance` 是**显式 host-admin 维护窗口**，不是普通
  task 可切换的 profile——任务 API 无切换入口，进入 / 离开只能由主机管理员
  执行 `scripts/activate_maintenance_instance.sh` / `scripts/deactivate_maintenance_instance.sh`。
  maintenance 复用 `bridge-workspace`（`.git/` 元数据可写、`.git/hooks/` 只读、
  GitHub 白名单网络、`.codex/` / `.agents/` / `.env` 只读），模板固定
  `on-request`，**永不使用 `danger-full-access`**；独立 CODEX_HOME
  （`~/.codex-deepseek-maintenance`）、port（`8323`）与 runtime，不自动复用
  local 的 ngrok 域名。窗口内任务 cwd 只允许 Bridge 仓库本身或其真实子目录
  （`validate_maintenance_cwd`，realpath 后判定；拒绝 `/`、`$HOME`、仓库祖先、
  仓库外项目、maintenance 实例状态与 maintenance CODEX_HOME；symlink 不能
  逃逸）。`BridgeCore.start` 按实例 scope 选择校验器，`/continue` 不接受
  `cwd` 不会扩权。
- **维护窗口的固定 endpoint 复用（host-admin 显式行为）**：为了让当前 Custom
  GPT Actions 在维护窗口仍走同一个固定 public endpoint，activate 脚本把现有
  `.bridge_api_key` / `.ngrok_domain` 作为**路径引用**写入 maintenance 实例
  配置——只引用路径，绝不复制 / 打印 secret / 域名内容；这是 host-admin 显式
  操作，不是 maintenance 默认模板行为（`create maintenance` 两个引用均为空），
  普通任务 / API 无此能力。
- **`.git/hooks/` 只读**：`bridge-workspace` 放行 `.git/` 元数据写入时，显式把
  `.git/hooks/` 设为 read，阻止任务向 hooks 目录投放可执行文件。限制说明：
  当前 Codex permission schema 只支持 read override、没有 deny 语义，因此该
  规则以 read override 表达（`.git/ = "write"` 与 `.git/hooks/ = "read"` 并列），
  不要假设存在"deny"级语义。

## 运行时与凭据路径（1.1.0）

- **三个稳定根**：state 在 `${XDG_STATE_HOME:-$HOME/.local/state}/local-codex-bridge/<instance>/`
  （实例配置 `600`、目录 `700`；维护窗口 pause marker 在
  `<state>/local/pause.marker`）；稳定 runtime 在
  `${XDG_DATA_HOME:-$HOME/.local/share}/local-codex-bridge/`（releases/ +
  `current` 原子符号链接，data root 与 releases 目录 `700`，只含 allowlisted
  tracked 文件，无 `.git`/tests/docs/backups/logs/secret；`.runtime-build-info`
  只写 release/HEAD/dirty/UTC time/version，无路径无 secret）；
  稳定凭据/路径引用在 `${XDG_CONFIG_HOME:-$HOME/.config}/local-codex-bridge/`
  （目录 `700`、文件 `600`）。runtime 放在非 Desktop 路径，避免 TCC 弹窗。
- **凭据路径引用迁移**：`install_runtime.sh --instance local` 发现 local 实例的
  `api_key_file` / `ngrok_domain_file` 仍指向 repo/Desktop 时，把文件复制进
  config root（`700`/`600`）并更新实例配置的**路径引用**；内容绝不打印、
  repo 原文件不删。启动脚本只按引用路径读取，secret 内容永远只走
  `BRIDGE_API_KEY` 环境变量或 `--log` 之外的运行内存。
- **supervisor / launchd 边界**：`run_local_supervisor.sh` 只接受显式
  `--instance local`（hpc/maintenance 按需手动起停，永不自动托管）；以 PID
  身份只读校验（`ps -p <pid> -o command=`）确认 bridge/ngrok 属于本项目后才
  操作，`pkill`/`killall` 全程禁用；网络/健康暂时失败不杀进程；维护窗口 pause
  marker（`<state>/local/pause.marker`）存在时停 local children 并等待、移除
  后恢复（supervisor 本身保持存活，哨兵不动，launchd 无 crash-loop）；哨兵
  `supervisor.enabled`（instance state runtime）移除或 TERM 时，先经
  `stop_ngrok_bridge.sh` 安全停子进程再退出。launchd 代理
  `com.local.codex-bridge.local` 的 KeepAlive 用 `PathState` 绑定哨兵绝对路径
  （哨兵在才存活），plist 不含 secret/domain，卸载/迁移不 kill 任何进程。
- **删除守卫**：runtime release 只允许删除
  `releases/release-<时间戳>-<head12>` 形态的真实目录（严格绝对路径 + 名称 +
  非 symlink + 托管 marker `.runtime-build-info`/`runtime.manifest` 校验），
  `current` 只做 `rm -f` 解链，绝不跟随删除；uninstall 默认保留 state、
  CODEX_HOME 与 config-root 凭据。

## 报告

- 项目已公开（`Xin-Jiaqi/local-codex-bridge`）。请通过 GitHub issues 报告安全问题；
  不要在 issue 中粘贴任何密钥。
