# Changelog

1.0.0 之前的条目根据本地源码与 2026-08-11 测试报告重建，日期为近似值；
仓库自 2026-08-12 起纳入 git 管理（v1.0.0，分支 `main`）。

## [1.1.0] - 2026-08-14

> 状态：**已发布（2026-08-14）**。tag `v1.1.0` 指向本次 release commit；
> v1.0.0 tag 仍指向 `fa82e91`，不移动。本条目来源分三类：`71274e5` 是
> v1.0.0 发布之后的文档 commit（已提交并 push 到 origin/main）；sandbox
> 模式 / permission profile / CI / 发布稿等修复（原 1.0.1 unreleased 条目
> 内容）；以及 1.1.0 新增的实例钉扎控制面隔离、cwd 守卫、多实例
> LaunchAgent 与迁移。全部工作统一归档到 1.1.0，避免把 post-release 修复
> 伪装成 v1.0.0 内容，也不虚构 1.0.1 已发布。

### 维护期集中修复（2026-08-14，maintenance instance 内完成，已随 v1.1.0 发布）

- **回归 1（真实故障）：`/health` 持续 500**。`_BridgeHTTPServer` 缺少
  `_config_overrides`，readiness 读取该属性时抛 AttributeError 返回 500。
  已保留并正式化 `self.httpd._config_overrides = self._config_overrides`
  wiring，并新增**真实** `BridgeHttpServer` 回归测试（断言
  `provider_config_ok`，避免 fake server 掩盖）；`/health` + `/ready` 统一
  为真实 readiness gate（app-server 存活 + provider secret 引用可读 +
  model/provider config 完整，三者齐备才 200）；openapi.yaml 同步为 9 个
  operation。
- **回归 2：stable runtime 漏 `config/`**。`install_runtime.sh` 的 tracked
  allowlist 纳入 `config/`（`config/bridge-workspace.example.toml`——真实
  runtime start 依赖的 bridge-workspace profile 示例）；新增
  installed-runtime dependency 测试：实际临时安装后必须包含该文件，且仍
  不含 `.git`/tests/docs/secrets/domain。
- **回归 3：deactivate 无 fail-safe**。`deactivate_maintenance_instance.sh`
  在 maintenance 已停、local 恢复失败时直接退出，公网控制面掉线。现在自
  maintenance stop 起武装 fail-safe rollback：local 恢复/本地 health/
  identity/公网 health 任一步失败 → 自动重新启动 maintenance 并验证
  maintenance/bridge-workspace/8323 + 公网 health；只有 rollback 也失败才
  报明确 DOUBLE FAILURE。无 broad kill（managed start path only）。
- **回归 4（实机暴露）：supervisor 误用不存在的 `/ready`**。local
  supervisor 轮询 `http://127.0.0.1:8321/ready` 返回 404，把本已成功启动
  且 local/public health 曾 OK 的 stack 判为 not ready，触发
  recovery/backoff。修复：readiness 只走 `/health` 并严格解析 JSON
  （`status=ok`、`ready=true`、`instance=local`、`mode=bridge-workspace`、
  `port=8321`），HTTP 200 本身不作为健康；共享 `bridge_health_ready`
  （supervisor_control 状态）同步改走 `/health`；fake curl 对 `/ready`
  返回 404（实机行为），supervisor 代码/测试不再依赖 `/ready`。
- **回归 5（实机暴露）：deactivate public handoff / rollback race**。
  local health identity 已 OK 但 public health 未及时 OK，rollback 被过早
  判 DOUBLE FAILURE（随后手工 start 立即成功，说明是时序而非真实故障）。
  修复：local health ready 后对 fixed public endpoint 用 bounded
  propagation polling（默认 45s/3s，env 可覆盖，绝不无限重试）；public
  失败 rollback 前先重建 pause marker 并显式停 local managed children 再
  启动 maintenance；rollback 自身 local health + public 各用 bounded budget
  （默认 20s + 40s），真正超时才 DOUBLE FAILURE。
- **single-writer host-ops lock**：新增 `scripts/host_ops_lock_lib.sh`——
  state root 下固定 `host-ops.lock` 目录，`mkdir` 原子获取；记录
  pid/operation/token/epoch（不含 secret）；同一父操作经导出 token 可重入；
  其他并发操作返回 BUSY；owner pid 已死时允许一次安全 stale cleanup；
  EXIT trap 自动释放。接入 activate maintenance、deactivate maintenance、
  activate_runtime_autorecovery、bootstrap_autorecovery.command，避免
  ChatGPT/自动化并发写同一控制面；普通 task API 不获得任何 host-op 能力。
- 测试：新增 `tests/test_host_ops_lock.py`（11 项：BUSY/reentrant/stale/
  release/trap/无 secret 记录）；`DeactivateRollbackTest` 8 项（成功、local
  start 失败→rollback、本地 health 失败→rollback、公网 health 失败→rollback、
  rollback 也失败→DOUBLE FAILURE）；health 回归 3 项（真实 server wiring +
  readiness）；installed-runtime config 依赖 1 项。全部 tmp/fake，不真
  launchctl、不 kill 真实进程。测试总量 248 = 244 通过 + 4 跳过。
  本次集中修复后更新：`DeactivateRollbackTest` 10 项（新增 public 延迟数秒
  后成功不 rollback、public 失败→pause+停 local→maintenance rollback 且
  rollback public 延迟后成功、真正超时才 DOUBLE FAILURE）；runtime
  supervisor 39→43（healthy `/health` 不触发 recovery、HTTP 200 但
  `ready=false` 触发、错误 identity 触发、静态禁止 `/ready`）。
  **测试总量 254 = 250 通过 + 4 跳过**。

### 实机 round-trip 验证（2026-08-14，已通过）

- 真实主机普通 Terminal 完整执行 `./scripts/activate_runtime_autorecovery.sh`：
  single-writer host-ops lock acquire / reenter / release 正常；stable
  runtime 安装（release `release-20260814T031607Z-8fe928ea856b`，原子
  current 切换）；per-instance LaunchAgent 唯一 label
  `com.local.codex-bridge.local` 装载；maintenance → local handoff 后
  local + public /health identity=local/bridge-workspace/8321。
- supervisor pid 43975 实机运行；对 managed bridge pid 20930 真实 TERM 后，
  supervisor 补起新 pid 21216，local + public health 恢复、ngrok 未被误杀；
  final status OK；随后自动重新进入 maintenance 并验证
  identity=maintenance/bridge-workspace/8323。
- marker：`AUTORECOVERY_ACTIVATION_OK YES`、`BOOTSTRAP_AUTORECOVERY_OK YES`。
- 本轮真实 round-trip 同时覆盖本文档记录的全部历史回归（回归 1-5），第二轮
  实机全部通过，未复现。细节见 `docs/release-validation-v1.1.0.md` 第 3 节。

### 运行时自动恢复：runtime + supervisor + per-instance LaunchAgent（2026-08-13）
- 新增 `scripts/install_runtime.sh` / `scripts/uninstall_runtime.sh`：稳定
  runtime 装到 `${XDG_DATA_HOME:-$HOME/.local/share}/local-codex-bridge/`
  （非 Desktop，避开 TCC）；staging + 原子 `current` 符号链接（`os.replace`
  rename 语义，BSD mv 会把符号链接当目录处理）；只复制 allowlisted tracked
  文件（`bridge` / `http_server` / 运行必需 `scripts/*.sh|.py`），不含
  `.git`/tests/docs/schemas/backups/logs/secret/domain；写非敏感
  `.runtime-build-info`（release/HEAD/dirty/UTC time/version/allowlist 数，
  无路径无 secret；`runtime.manifest` 保留为兼容产物）；data root 与
  releases 目录 `700`；`--dest DIR` 支持 temp-dir 测试；保留最近 2 个
  release，删除走严格守卫（release 名称正则 + 绝对路径 + 非 symlink +
  托管 marker 文件），`current` 只解链。仅支持 `--instance local`
  （hpc/maintenance 拒绝）。
- install 同时做稳定凭据路径引用迁移：local 的 `api_key_file` /
  `ngrok_domain_file` 若指向 repo/Desktop，复制到
  `${XDG_CONFIG_HOME:-$HOME/.config}/local-codex-bridge/`（目录 700、文件
  600）并安全更新实例配置引用（内容绝不打印、repo 原文件不删）。uninstall
  只删带托管 marker 的 managed release（拒绝任意路径），默认保留 state、
  CODEX_HOME 与 config-root 凭据。
- 新增 `scripts/run_local_supervisor.sh`（仅 explicit `--instance local`，
  前台运行）：读 instance state runtime 下 `supervisor.enabled` 哨兵与
  `supervisor.pid`；调用现有 start 后按 PID 身份监控 bridge/ngrok，任一真
  退出则节流/退避补起（指数退避封顶 60s）；网络闪断由存活 ngrok 自重连，
  健康/网络暂时失败不杀进程；维护窗口读 local state 下 `pause.marker`
  （`<state>/local/pause.marker`）：存在时停 local children 并等待，marker
  移除后恢复——supervisor 本身保持存活（哨兵不动，launchd 无 crash-loop）；
  哨兵消失或 TERM/INT 时经 stop_ngrok_bridge.sh 安全停子进程、清 pid、exit 0；
  异常非 0 交给 launchd。supervisor/子进程均以 PID identity 只读校验，无
  pkill/killall。
- 新增 `scripts/supervisor_control.sh`（sourceable + CLI status/enable/disable/
  restart）：唯一读写哨兵与 supervisor.pid 的 helper；enable = 哨兵 +
  launchd kickstart（agent 已装）/ legacy start 回退；disable = 移除哨兵并
  等待 supervisor 退出（--stop 时在无 supervisor 情况下停子进程）。
- LaunchAgent 改为 per-instance `com.local.codex-bridge.local`：ProgramArguments
  指向非 Desktop runtime `current/scripts/run_local_supervisor.sh --instance
  local`；RunAtLoad=true；KeepAlive 用 PathState 绑定实例状态 runtime 下绝对
  路径 `supervisor.enabled`（非无条件 true；maintenance 正常暂停时哨兵保持，
  supervisor 存活，不会 crash-loop）；ThrottleInterval=10；日志落 local
  instance state runtime（`launchagent.out.log` / `launchagent.err.log`）；
  plist 无 secret/domain、无 AbandonProcessGroup（supervisor 前台受 launchd
  管理）。install 前验证 runtime marker（`.runtime-build-info` /
  `runtime.manifest` + supervisor 脚本存在）；安全迁移/备份 legacy
  `com.local.codex-bridge` 与旧 Desktop plist（精确 bootout 指定 label +
  `.bak-<ts>` 备份，不 pkill/killall、无 broad launchctl）；hpc/maintenance
  拒绝自动托管；uninstall local 同时移除哨兵；status 新增 runtime source /
  current release / supervisor enabled+pid / pause 状态，仍不显示敏感值。
- maintenance 协同（pause marker）：activate 在停 local 前创建
  `<state>/local/pause.marker`（哨兵保持，前台 supervisor 存活并停住 local
  children），再停 local/起 maintenance；fail-safe rollback 自交接点起武装，
  rollback 先清 pause marker 再恢复进入窗口前的 enabled/disabled 状态并恢复
  local（agent 已装走 kickstart，否则 legacy start；原为 disabled 则 local
  保持停止）；成功进入窗口后 marker 保留。deactivate 停 maintenance 后清
  pause marker，优先让 launchd supervisor 恢复（已在运行则自行恢复 children，
  否则哨兵 + kickstart；agent 未装/不可用时 fallback 到现有 local start），
  验证 local/public health；无 marker 的旧窗口按 legacy 恢复，marker 缺失时
  默认恢复 local。全程不碰 hpc。
- 测试：新增 `tests/test_runtime_supervisor.py`（35 项：installer allowlist/
  原子 current/`.runtime-build-info` 无 secret+version/`--dest`/根目录 700/
  prune+uninstall marker 守卫/凭据迁移权限与不泄露、plist 非 Desktop +
  RunAtLoad + PathState、supervisor enable-disable/bridge crash/ngrok crash/
  网络失败不误杀/退避/TERM/哨兵、pause-resume/pause-at-startup/runtime-copy 无 Desktop 依赖、legacy 精确迁移、不碰 hpc、
  maintenance pause-before-stop/rollback 恢复/deactivate 恢复/旧环境兼容、
  uninstall 保留 state+凭据；11 项 live 需 `ps`，无 `ps` 的 seatbelt 沙箱内
  跳过，CI/普通终端运行）；`test_maintenance_instance.py` 更新到 40 项
  （pause marker 交接顺序、动态 rollback marker 清理+哨兵恢复、activate
  marker 断言）。
- 文档：README 新增"运行时部署与自动恢复"（一次性 host-admin 部署命令、恢复
  矩阵、验证边界）；SECURITY 新增"运行时与凭据路径"（三个稳定根、路径引用
  迁移、supervisor/launchd 边界、删除守卫）；`docs/instance-isolation.md`
  新增 runtime/supervisor 布局与恢复矩阵（含 pause marker）；`docs/release-process.md`
  新增一次性 runtime 部署步骤；`docs/zhihu-v1.md` 同步最新事实 + 短自动恢复段
  （仍写 v1.1.0 未发布）。**launchd 实机装载未在本次 maintenance 会话执行**
  （`~/.local/share` 在沙箱写权限外），实现与 temp-dir 测试已完成。

### 文档 / operations（1.1.0）
- 新增 `docs/chatgpt-codex-operating-policy.md`（v1.0，2026-08-13）：Custom GPT
  Instructions 的仓库版本化副本，ChatGPT 主导 planning / reasoning / review，
  Codex 作为本机执行器负责精确文件修改、shell、测试、git/gh 与 runtime 验证；
  含 6 步 task lifecycle（plan → execute → observe → review → precise
  follow-up → final report）与优先级规则（用户当次明确要求 > Custom GPT
  Instructions > 本 policy 仓库副本 > 一般默认）。目标包括降低重复 Codex API
  消耗：复用 native thread、不重复读取/解释、follow-up 小而精确、仅本机事实
  类核查才用 Codex reviewer。运行时行为仍由 GPT Builder 的 Instructions 决定，
  仓库文件不会自动同步回 GPT Builder。
- 更新 `docs/chatgpt-codex-operating-policy.md` 至 v1.1：强化 Long-running
  task completion protocol（running 中持续 codexObserve 到终态、硬限制下显式
  `PENDING` 并记录 thread_id/turn_id/项目/cwd/下一检查点、配置了 unattended
  monitor 的项目由 LaunchAgent + monitor 离线接管、终态后停止自动 Codex 调用、
  新窗口经 checkpoint/monitor log/native thread 恢复、离线通知由本机
  monitor/email 完成）。
- 新增 `docs/unattended-automation.md`：项目级通用设计（framework）——ChatGPT
  在线规划/监督，离线后 LaunchAgent 按固定周期运行 monitor；monitor 读
  checkpoint/state/log 判断 running/terminal/blocked，必要时调用本机 Codex
  执行窄范围任务；terminal 终态首次出现时收集摘要并邮件通知，随后写 terminal
  marker，无论邮件成败都停止自动 Codex 调用；邮件凭据只放 macOS Keychain；
  rearm 清 marker 且不改业务状态；动态识别 active/recent job，不硬编码 job id；
  未知高风险写 BLOCKED 等用户/ChatGPT。具体项目私有配置（label、频率、邮件
  通道、脚本路径等）不写入本公共仓库文档。

> 版本口径：这是 **minor (1.1.0)**，不是 patch，也不是 major。原因：
> HTTP/Actions 公共接口保持兼容（8 个 operation、request/response shape、
> 错误格式不变），但部署/运维模型获得新能力：实例钉扎的控制面（`local` /
> `hpc` / `maintenance` 独立 CODEX_HOME / port / runtime）、任务 cwd 守卫、
> 多实例 LaunchAgent、`migrate-current` 与 maintenance 维护窗口，且默认
> 安全边界更强（hpc / maintenance 模板永不使用 `danger-full-access`）。
> 按 semver：无 breaking change 不升 major；
> 新增能力 + 更强默认值属于 minor，不夸大为功能型 major。

### 控制面隔离：实例钉扎（2026-08-13）
- 移除第一版 repo 内 `.bridge-control/active_profile` 可切换 profile 机制
  （`bridge_profile*.sh` 与对应测试一并删除）；运行态控制面移出任务工作区。
- 状态根：`${XDG_STATE_HOME:-$HOME/.local/state}/local-codex-bridge/<instance>/`
  （目录 `700`、配置 `600`），不随仓库分发、不入 git。
- Bridge 进程启动时经 `BRIDGE_INSTANCE`（默认 `local`）钉扎到唯一实例；没有
  任务 API / helper 能切换运行中实例。实例配置只含非 secret 字段：
  `name` / `mode` / `approval_policy` / `network_access` / `codex_home` /
  `port` / `runtime_dir` / `api_key_file` / `ngrok_domain_file`（后两者只存
  路径引用，绝不复制 secret 内容）。
- `local`：`bridge-workspace` + 自动化安全审批策略 + GitHub 网络；`hpc`：
  独立 CODEX_HOME / port / runtime、`workspace-write` + `on-request` +
  网络开启，模板**永不**是 `danger-full-access`（admin 脚本与测试强制）。
- 唯一实例状态写入者是 `scripts/bridge_instance.sh`
  （list / show / create / update / verify / migrate-current）；
  `scripts/bridge_instance_lib.sh` 与任务脚本只读。任务 helper / tests /
  迁移不得写实例状态或 `.bridge_sandbox_mode`（回归扫描测试强制）。

### 任务/控制面边界：cwd 守卫（2026-08-13）
- 新增 `bridge/workspace_guard.py`：`/start`（以及 `/continue` 若带 cwd）接受
  任务 cwd 前先对真实路径做 canonicalize；`local` / `hpc` 拒绝 `$HOME` 及其
  祖先、Bridge 仓库（相等 / 祖先 / 内部）、当前实例状态根、实例 CODEX_HOME
  （相等 / 祖先 / 内部）；symlink 先解析，无法绕过。
- 错误为结构化 `TaskCwdError`（通用 reason + category，不泄露私有路径）；
  正常兄弟项目（如 `$HOME/Desktop/some-project`）不受影响。
- 目标：任务不能选 `$HOME` 或本 Bridge 仓库当"宽工作区"后改写控制面文件；
  Bridge 维护是显式 admin / 维护工作流，不静默放行。19 项离线测试
  （含 symlink 绕过、祖先 / 子孙、真实 start 路径接线）。

### 安全加固（2026-08-13）
- `bridge-workspace` profile 增加 `.git/hooks/ = "read"`（与
  `.git/ = "write"` 并列）：放行 `.git/` 元数据写入时 hooks 目录仍只读。
  当前 Codex permission schema 只支持 read override、没有 deny 语义，
  已按 schema 能力如实实现（不虚构 deny 规则）。
- hpc 模板策略固定 `workspace-write` + `on-request`，永不生成 / 接受
  `danger-full-access`；`local` 保持最小授权（`bridge-workspace`）。
- 实例配置 / 模板 / 状态不含 secret；API key 与 ngrok 域名仍由外部文件 /
  env 提供，`api_key_file` / `ngrok_domain_file` 只存路径。

### HOST-ADMIN：maintenance 维护窗口（2026-08-13）
- 新增第三个固定实例 `maintenance`（`local` / `hpc` / `maintenance` 三实例）：
  `bridge_instance_valid` 白名单、list / collision / start / stop / status /
  install / uninstall / verify / update / create 循环全部同步包含。
  maintenance 模板 = `bridge-workspace` + `on-request` + 网络开启 +
  `~/.codex-deepseek-maintenance` + port `8323` + 独立 runtime；
  **永不映射 `danger-full-access`**（admin 脚本与测试强制），也**不自动继承**
  local 的 legacy ngrok domain / public endpoint（无 `ngrok_domain_file` 时
  启动 fail-closed）。
- maintenance 是**显式 host-admin 维护窗口**，不是普通 task 可切换的 profile：
  任务 API / helper 无切换入口。新增
  `scripts/activate_maintenance_instance.sh` / `scripts/deactivate_maintenance_instance.sh`：
  activate = 先验证 local 实例 → 准备 `~/.codex-deepseek-maintenance`
  （不存在时只复制 `~/.codex-deepseek/config.toml`，绝不复制 threads/history/
  cache；目录 700、config 600）→ 用 `--codex-home` 兼容参数对 maintenance
  config 做与 bridge-workspace 相同的 legacy-sandbox/profile verify/migration
  → create/verify maintenance 实例 → **显式**把 `.bridge_api_key` 与
  `.ngrok_domain` 作为**路径引用**写入 maintenance 配置（只引用路径，绝不
  复制/打印 secret/domain 内容；不是默认模板行为）→ 先停 local 再起
  maintenance → 验证 local/public health、`instance=maintenance`、
  `mode=bridge-workspace`、`port=8323`；不使用 pkill/killall，不碰 hpc /
  Para/Japan / 远端 jobs。deactivate = 停 maintenance、起 local、验证
  local/public health；**不删除** maintenance state 与 CODEX_HOME（下次维护
  直接复用）。
- maintenance cwd 守卫：新增 `validate_maintenance_cwd`——只接受 Bridge 仓库
  根本身或其真实子目录（realpath 后判定，symlink 不能逃逸）；拒绝 `/`、
  `$HOME`、仓库祖先、仓库外项目、maintenance 实例状态根与 maintenance
  CODEX_HOME。`BridgeCore.start` 按实例 scope（`build_cwd_guard` 的 `scope`）
  选择校验器；`/continue` 依旧不接受 `cwd`，不会扩权。local/hpc 现有守卫
  一字不弱化（既有 19 项 workspace guard 测试原样通过）。
- `/health` 增加只读元数据：`instance` / `mode` / `port`（openapi.yaml 同步；
  仍 8 个 operation，全部 `x-openai-isConsequential: false`）。
- `scripts/migrate_codex_home_permissions.py` 新增 `--codex-home DIR` 兼容参数
  （显式 CODEX_HOME：默认 `--config` 为 `<DIR>/config.toml`、额外扫描
  `<DIR>/config`），maintenance activate 用它做迁移/verify，不硬编码路径；
  现有 `--config` / `--config-dir` / `--project-root` 行为不变。

### 运维：多实例 LaunchAgent 与域隔离（2026-08-13）
- `install / uninstall / status_launch_agent.sh` 支持 `--instance NAME`：
  显式实例安装用 `com.local.codex-bridge.<name>` label，plist 内注入
  `BRIDGE_INSTANCE=<name>`；不带 `--instance` 保持 legacy
  `com.local.codex-bridge`（向后兼容，可继续工作直到迁移）。
- start / stop / status 从选中实例派生 port / runtime / CODEX_HOME /
  approval / network；`local` + `hpc` 并发仅当 port 与 runtime 互不相同，
  任何碰撞都 fail-closed（start / stop / status / install / verify 均拒绝）。
- hpc 不自动复用 local 的 ngrok 域：`ngrok_domain_file` 缺省时 hpc 启动给出
  明确报错（不做 local 域回退），local-only 模式不受影响。
- 本条目不修改任何已安装 LaunchAgent；迁移由用户显式执行。

### 迁移：migrate-current / verify（2026-08-13）
- `scripts/bridge_instance.sh migrate-current --dry-run / --apply`：把 legacy
  单例设置迁移进 `local` 实例（模式 / 审批 / 网络 / CODEX_HOME / port /
  runtime + 两个路径引用），apply 前先备份（同目录、600），不复制 / 打印
  secret；legacy `.bridge_sandbox_mode` / `.bridge_api_key` / `.ngrok_domain`
  保持不动。
- `verify NAME`：校验模式 / 审批 / 网络 / 绝对路径 / port / runtime、
  引用文件存在、实例间碰撞。
- 缺实例配置时 start / status / stop 回退 legacy 单例行为并打印 warning
  （明确的 deprecation 路径，迁移前一切照旧）。

### 测试（1.1.0）
- 离线单测共 **195 项**：`test_instance_isolation.py` 41（实例隔离 / 策略 /
  无 active-profile 切换 / admin-only 写入 / legacy fallback / 无 secret /
  权限 700/600 / LaunchAgent 标签 / 碰撞 / 域隔离 / migrate / verify /
  maintenance 模板 / 永不 danger-full-access）、`test_workspace_guard.py` 19、
  `test_maintenance_instance.py` 40（maintenance 模板/策略/域隔离/碰撞/
  cwd 守卫 scope / `/start` maintenance scope / health 元数据 /
  activate-deactivate 脚本不变量 / supervisor 交接与 rollback / 无 secret literal）、
  `test_runtime_supervisor.py` 29（runtime install/uninstall allowlist / 原子
  current / manifest 无 secret / prune 守卫 / 凭据迁移权限与不泄露 / plist
  PathState / supervisor enable-disable、bridge+ngrok crash 补起、网络失败不
  误杀、退避、TERM、哨兵 / maintenance 协同 / uninstall 保留；8 项 live 需
  `ps`，无 `ps` 的沙箱内跳过，CI/普通终端运行）、
  `test_git_automation.py` 28 离线 + 1 可选集成、
  `test_migrate_codex_home_permissions.py` 17、`test_pid_guard.py` 10
  （3 项 live-process 需 `ps`，无 `ps` 的沙箱内跳过）、`test_sandbox_mode.py`
  7、`test_config_propagation.py` 3。
- 回归扫描：测试强制"任务脚本 / 测试 / 迁移不得写实例状态与
  `.bridge_sandbox_mode`"、hpc 永不 danger-full-access、实例配置只含 schema
  键、migrate/verify 输出不含 secret 值。

### 发布准备（1.1.0）
- 版本字段统一为 `1.1.0`：`bridge.__version__`、`openapi.yaml` `info.version`、
  app-server initialize `clientInfo`（`local-codex-bridge-core/1.1.0`）与相关测试断言一致；
  v1.0.0 tag 不动，不虚构 1.0.1 已发布。
- 新增 GitHub Actions CI（`.github/workflows/ci.yml`）：Ubuntu job 跑安全离线
  tests、`py_compile`、`bash -n`、plist lint（`plistlib`，跨平台）；macOS job
  补 `plutil -lint`。无需 secret，不调用 DeepSeek/app-server/网络；
  `RUN_SANDBOX_TESTS=1` 的 Seatbelt 集成验证明确排除。
- 新增 `CONTRIBUTING.md`（本地测试、代码风格、secret 规则、部署文件清单、PR checklist）、
  `docs/release-process.md`（发布 checklist 与 tag 纪律）、
  `docs/v1-development-notes.md`（脱敏工程历史）、`docs/xhs-v1.md`（小红书发布稿，
  无虚构截图）、`docs/content-publishing-checklist.md`（知乎/小红书发布前检查清单：
  配图状态、截图隐私、时点 final sync、人工发布流程）。
- 测试口径：离线单测 195 项（`test_config_propagation.py` 3、`test_sandbox_mode.py` 7、
  `test_instance_isolation.py` 41、`test_workspace_guard.py` 19、`test_maintenance_instance.py` 40、
  `test_runtime_supervisor.py` 29（21 离线 + 8 live 需 `ps`）、
  `test_git_automation.py` 28 离线 + 1 可选集成、`test_migrate_codex_home_permissions.py` 17、
  `test_pid_guard.py` 10）+ 1 项可选 sandbox 集成验证（需普通 Terminal + `RUN_SANDBOX_TESTS=1`）。

### 发布后增强：Bridge 沙箱模式开关（2026-08-12）
- `BRIDGE_SANDBOX_MODE` 三档：`workspace-write`（默认，V1 边界不变）/
  `bridge-workspace`（permission profile：项目 `.git` 元数据写入 + GitHub 白名单网络，
  无全盘写权限）/ `danger-full-access`（显式选择，与直接 CLI 沙箱一致）。
- `BRIDGE_NETWORK_ACCESS=true`：`workspace-write` 下按官方
  `[sandbox_workspace_write] network_access = true` 开启网络。
- `http_server/server.py`：`build_config_overrides()` 按环境变量生成 app-server `-c`
  参数；`bridge-workspace` 为自包含模式——完整 profile（filesystem + network 规则）
  经 `-c` dotted TOML override 注入，无需修改 `$CODEX_HOME/config.toml`。
- 已按官方规则修正（见下「发布后修正：bridge-workspace 纯 profile」）：beta
  permission profile 不能与 legacy `sandbox_mode` / `[sandbox_workspace_write]`
  混用；bridge-workspace 的 `-c` 注入为纯 profile，且专用 CODEX_HOME 必须迁移干净。
- `config/bridge-workspace.example.toml`：profile 参考模板（与 `-c` 注入规则一致，
  tests 保证同步）。
- 新增 `scripts/verify_bridge_git_automation.sh`：真实 `codex sandbox` 验证
  `bridge-workspace`（与 Bridge 相同的 `-c` 注入）下 git init/add/commit 成功、
  `workspace-write` 拒绝（复现 Codex 0.147.0 protected `.git` 限制）；
  `--network` 追加只读 GitHub 连通性检查。
- 新增 `tests/test_git_automation.py`：沙箱模式/网络配置传播与 profile 模板授权
  范围离线校验（12 项）；真实 `codex sandbox` 集成验证（`bridge-workspace`
  git commit 成功 / `workspace-write` 拒绝）为可选（`RUN_SANDBOX_TESTS=1`，
  需普通 Terminal）。
- `install_launch_agent.sh` 支持 `--sandbox-mode` / `--network-access`，注入生成的 plist
  `EnvironmentVariables`（launchd 登录自启可直接用 `bridge-workspace`）。
- README/SECURITY 同步：三种模式边界、`.git` protected path 限制、GitHub 白名单、
  `danger-full-access` 风险说明。

### 发布后增强：持久模式文件（2026-08-12）
- `.bridge_sandbox_mode`（gitignored，`chmod 600`）：项目本地持久模式文件，
  生效优先级 `BRIDGE_SANDBOX_MODE` env > 文件 > 默认 `workspace-write`；
  `scripts/start_ngrok_bridge.sh` / `status_launch_agent.sh` 均读取。
- 新增 `scripts/bridge_mode_lib.sh`（模式解析共享库）与
  `scripts/set_bridge_sandbox_mode.sh`（设置/查看/清除，校验三档值，无 secret）。
- 新增 `tests/test_sandbox_mode.py`：模式优先级、setter 校验/写 600/清除、
  本地模式文件生命周期（6 项离线测试）。
- 无人值守重启原语（`restart_bridge.sh`）在发布前审查中移除：实测确认从 Bridge
  沙箱内部发起的任何进程（含 `launchctl kickstart` 拉起的 launchd job）都会继承
  seatbelt profile（exit 126 / 无网络坏 Bridge），"Bridge 内自重启"不可靠；
  切换模式请用 `set_bridge_sandbox_mode.sh` 持久化，再在普通 Terminal 执行
  stop/start 或重跑 LaunchAgent。

### 发布后修正：bridge-workspace 纯 permission profile（2026-08-12）

按已核实的 Codex 官方规则修正实现（此前"`-c` 的 default_permissions 优先于文件里的
`sandbox_mode`"的假设作废）：

- **不混用 legacy 沙箱键**：beta permission profile 与旧 `sandbox_mode` /
  `[sandbox_workspace_write]` 不能共存——任何 loaded config 含它们，Codex 就退回
  旧 sandbox 并忽略 `default_permissions`。现在 `bridge-workspace` 的 `-c` 注入
  **绝不包含** `sandbox_mode` / `sandbox_workspace_write`（`tests/test_git_automation.py`
  强制：profile 模式无 legacy 键、legacy 模式无 profile 键）；`workspace-write` /
  `danger-full-access` 继续走 legacy 机制。
- **0.147.0 协议级要求（实测补丁）：thread/start 显式选择 profile**。命名
  permission profile 除了 `-c` 注入外，还必须在 `thread/start` 参数中显式选择：
  bridge-workspace 下每个新 thread 均携带
  `config: {"default_permissions": "bridge-workspace"}`，且 thread/turn 参数绝不
  包含 legacy `sandbox` / `sandboxPolicy` 字段（其枚举无 profile 值，出现即退回
  旧沙箱并禁用 profile）；legacy 两档不发送 `default_permissions`。
  `tests/test_git_automation.py` 新增逐模式 app-server 参数断言。
- **profile 形态**：`default_permissions="bridge-workspace"` +
  `[permissions.bridge-workspace]`，`extends=":workspace"` 保留 baseline protections；
  `:workspace_roots` 下 `"."` 可写、`.git/` 可写、`.codex/` / `.agents/` / `.env`
  只读；`":minimal"` 可读、`:tmpdir` / `:slash_tmp` 可写；network `enabled=true`
  + domains allowlist：`github.com`、`*.github.com`、`api.github.com`、
  `ssh.github.com`、`*.githubusercontent.com`、`objects.githubusercontent.com`、
  `raw.githubusercontent.com`（无 `"*" = "allow"`）。
- **启动守卫**：bridge-workspace 下若专用 CODEX_HOME（`config.toml` 或
  `config/*.toml`，以及项目 `.codex/config.toml`）仍含 legacy 沙箱键，server
  **拒绝启动**（exit 2，给出迁移指引），不再降级运行；`start_ngrok_bridge.sh`
  在启动前用迁移脚本 `--verify --project-root <root>` 预检，与 server guard 的
  扫描范围一致（`CODEX_HOME/config.toml`、`CODEX_HOME/config/*.toml`、
  项目 `.codex/config.toml`），避免"start 预检通过、server 再拒绝"的假通过；
  项目 `.codex` legacy 冲突只报告、要求用户手动处理，迁移 `--apply` 从不改项目配置。
- **迁移脚本**：新增 `scripts/migrate_codex_home_permissions.py`（替代
  `ensure_bridge_permissions.sh`，已删除）：`--dry-run` / `--verify` / `--apply`，
  只移除 legacy `sandbox_mode` 键与 `[sandbox_workspace_write]` 表、写入
  `default_permissions = "bridge-workspace"`、安装/更新 `[permissions.bridge-workspace]`
  块；其余配置与注释逐字节保留；输出只打印键名/表名与模板内容，**不打印任何值**
  （API key 等 secret 永不出现在输出）；apply 写同目录 `config.toml.bak`
  （chmod 600），幂等（重复 apply 报 already up to date）；`--project-root`
  与 `--config-dir` 支持只读扫描项目 `.codex/config.toml` 与
  `$CODEX_HOME/config/*.toml`（与 server guard 范围一致，新增 8 项测试）。
- **PID 身份守卫**：新增 `scripts/pid_guard_lib.sh`；start/stop 在 SIGTERM/
  SIGKILL 或复用 PID 前，用 `ps -p PID -o command=` **只读校验**进程身份确实
  是本项目管理的 Bridge（`python3 -m http_server` + 项目 `.runtime` 上下文）或
  ngrok（`ngrok http 8321`）；校验失败只报 stale/unmanaged 并清理 pid 文件，
  **绝不 kill**；全程不使用 pkill/killall。新增 `tests/test_pid_guard.py`
  （10 项：7 项纯匹配 + 3 项 live-process，后者在无 `ps` 的沙箱内跳过）。
- **CI 供应链**：`.github/workflows/ci.yml` 的 actions 固定官方 full SHA
  （checkout v4.4.0 / setup-python v5.5.0，带版本注释），不引入第三方 action。
- **外部产物隔离**：`deliverables/`（其他仓库维护产物）加入 `.gitignore`，
  CONTRIBUTING/release-process 的 scan checklist 明确排除、永不提交。
- **Git 代理隔离**：bridge-workspace 下 app-server 子进程环境用 `GIT_CONFIG_*`
  把 `http.https://github.com.proxy` / `http.proxy` 覆盖为空并丢弃 `http_proxy` /
  `https_proxy` / `all_proxy` 等环境变量，git 直连 GitHub，不依赖本机全局代理
  （如 Clash 等本机代理）；`~/.gitconfig` 不被修改，仅限 Bridge 子进程环境。
- `verify_bridge_git_automation.sh`：注入的 profile flags 若含 legacy 键直接失败；
  集成验证逻辑不变（真实 `codex sandbox`，需普通 Terminal）。
- 测试：`test_git_automation.py` 扩到 23 项离线（纯 profile 约束、child env 代理
  清理、legacy 键检测、模板授权范围），新增 `test_migrate_codex_home_permissions.py`
  （8 项：dry-run/verify/apply、幂等、备份 600、secret 不泄漏）。离线共 39 项 +
  1 项可选 sandbox 集成验证。
- README/SECURITY 同步：专用 CODEX_HOME 不得混用 legacy sandbox keys、迁移步骤、
  GitHub 白名单主机、Git 代理隔离说明。

## [1.0.0] - 2026-08-12

### 版本与发布准备
- 版本统一为 `1.0.0`：`bridge/__init__.py`、app-server initialize `clientInfo`、
  `openapi.yaml` metadata 一致（已打 annotated tag `v1.0.0` 并发布 GitHub Release）。
- README 重写为 V1 工程入口（What/Why/Architecture/Quick Start/Actions/Security/
  Configuration/Start-Stop/Project Structure/Known Limitations/V1 Status）。
- 新增 `SECURITY.md`、`CHANGELOG.md`、`docs/zhihu-v1.md`（知乎初稿，未发布）。
- 新增 `LICENSE`（BSD-3-Clause，`Copyright (c) 2026, Jiaqi Xin`，与其它自有仓库一致）。
- 新增 launchd LaunchAgent 开机自启：plist 模板 + install/uninstall/status 脚本
  （登录自动启动、幂等复用 `.runtime` PID、卸载不影响运行中进程）。

### 发布后整理（2026-08-12）
- 测试数字口径统一：离线单测 2/2（`test_config_propagation.py`）为当前可复现；
  集成测试 2026-08-11 实测 core 5/5、actions 7/7、HTTP API 11/11、公网 tunnel 6/6
  为历史记录；当前 `test_http_api.py` 含 12 个唯一场景，与历史记录差 1 项，未复跑确认。
- `docs/zhihu-v1.md` 编辑级重写（结构/文风/事实口径，状态更新为已发布）。
- README 发布状态、测试口径与实测环境措辞更新。

### 安全与配置
- `.gitignore` 强化：`.bridge_api_key`、`.public_url`、`.ngrok_domain`、`openapi.ngrok.yaml`、
  `.runtime/`、`*.pid`、`*.log`、`__pycache__/` 全部不入库。
- ngrok 固定域名外部化：支持 `NGROK_DOMAIN` 环境变量或 `.ngrok_domain` 文件（gitignored），
  不再硬编码在脚本/README。
- 代码与文档中的个人绝对路径硬编码全部移除：`CODEX_BIN` 默认走 `command -v codex`，
  `CODEX_HOME` 默认 `$HOME/.codex-deepseek`。

### OpenAPI 三件套策略
- `openapi.yaml`：公开模板（`servers.url` 为占位符，测试强制校验）。
- `openapi.ngrok.yaml`：本地部署副本（真实域名，gitignored，不随仓库发布）。
- `tests/test_http_api.py` 同步：部署副本改为可选校验（缺失/占位均不视为失败），
  并新增 `info.version == 1.0.0` 校验。

### 脚本修复
- `scripts/start_ngrok_bridge.sh`：修复 `die()` 在定义前被调用的问题；
  `CODEX_BIN` 解析改为 `command -v codex || true`，避免 PATH 未命中时被 `set -e` 静默终止。

### 清理
- 移除不属于项目发布的内容：`cleanup-disk.sh`（个人磁盘清理）、`start_public_bridge.sh`
  （cloudflared quick tunnel 变体）、`smoke_test.py`（被集成测试 + 离线单测取代）、
  旧 `*_TEST.md` 报告、根目录历史日志与 stale 产物。

## [0.2.0] - 2026-08-11（重建）

- `bridge/` core client：`codex app-server` 子进程生命周期、JSON-RPC 2.0 over stdio、
  通知分发、进程退出处理。
- 7 个 HTTP action：start / continue / observe / steer / interrupt / list / read。
- 集成测试 PASS（2026-08-11，重建记录）：core 5/5、actions 7/7、HTTP API 11/11。

## [0.1.0] - 2026-08-11（重建）

- 初始 HTTP bridge：`/health`、`/start`、`/observe`、`/continue`，OpenAPI 模板 0.1.0。
