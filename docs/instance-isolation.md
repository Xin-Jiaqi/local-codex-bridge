# 实例隔离（Instance Isolation）

> 面向维护者的 1.1.0 架构说明：实例钉扎的控制面、状态布局、迁移、并发规则与
> 回滚。任务面（HTTP API）行为不变；本文只描述控制面。

## 1. 为什么需要

v1.0.0 的 Bridge 只有一个"当前配置"：模式、网络、CODEX_HOME、端口全部来自仓库
根目录的 legacy 文件与默认值。这带来两个问题：

1. **任务面能碰到控制面**：如果任务把 `$HOME` 或本仓库当作工作区，就有机会改写
   `.bridge_sandbox_mode`、`.runtime/` 或仓库内脚本——控制面与任务面没有边界；
2. **没有隔离的第二种用途**：coding/release 自动化（需要 git+GitHub）与交互式
   更宽任务不能安全共存——前者要最小授权，后者要更宽但独立、互不污染。

1.1.0 的解法：**实例钉扎（instance-pinned）**。控制面状态移出任务工作区，Bridge
进程启动时钉扎到唯一命名实例（`local` / `hpc` / `maintenance`），任务侧没有切换
实例的接口。`maintenance` 是第三个固定实例：Bridge 自身仓库维护的显式
host-admin 维护窗口，不是普通 task 可切换的 profile。

## 2. 架构与状态布局

```
控制面（任务不可写）
${XDG_STATE_HOME:-$HOME/.local/state}/local-codex-bridge/
├── local/
│   ├── instance.conf        # 非 secret 配置，chmod 600
│   ├── pause.marker         # maintenance 窗口保持信号（存在 = local 暂停托管）
│   ├── backups/             # update/migrate 前的备份，chmod 600
│   └── runtime/             # bridge.pid / ngrok.pid / public_url / 日志 /
│                            # supervisor.enabled 哨兵 / supervisor.pid / launchd 日志
├── hpc/
│   ├── instance.conf
│   ├── backups/
│   └── runtime/
└── maintenance/
    ├── instance.conf
    ├── backups/
    └── runtime/
```

- 目录一律 `700`，配置文件 `600`；状态根不在仓库内、不入 git。
- 实例配置只含非 secret 字段：`name`、`mode`、`approval_policy`、
  `network_access`、`codex_home`、`port`、`runtime_dir`、`api_key_file`、
  `ngrok_domain_file`（后两个只存**路径引用**，secret 内容仍在外部文件 / env）。
- **没有可切换的 active profile**：仓库内不存在"当前 profile"指针；第一版
  `.bridge-control/active_profile` 机制已删除。`BRIDGE_INSTANCE` 只在进程启动时
  生效（默认 `local`），运行中不可切换。

## 3. 三个实例的默认策略

| | `local` | `hpc` | `maintenance` |
|---|---|---|---|
| 定位 | 日常 coding / release，最小授权 | 独立远端运维：交互式 / 更宽范围 | **Bridge 自身仓库维护**（host-admin 维护窗口） |
| mode | `bridge-workspace` | `workspace-write`（**永不 `danger-full-access`**） | `bridge-workspace`（**永不 `danger-full-access`**） |
| approval_policy | `on-request` | `on-request` | `on-request` |
| network_access | `true`（GitHub 白名单） | `true` | `true`（GitHub 白名单） |
| codex_home | `~/.codex-deepseek` | `~/.codex-deepseek-hpc` | `~/.codex-deepseek-maintenance` |
| port / runtime | `8321` / `<state>/local/runtime` | `8322` / `<state>/hpc/runtime` | `8323` / `<state>/maintenance/runtime` |
| 公网域名 | `ngrok_domain_file`；legacy `.ngrok_domain` 仅 local 回退 | 必须显式 `ngrok_domain_file`；**不自动复用 local 域名** | 必须显式 `ngrok_domain_file`；**不自动复用 local 域名** |

hpc / maintenance 没有配置 `ngrok_domain_file` 时：启动给出明确报错（不做
local 域回退），local-only 模式不受影响。maintenance 默认模板**不**带
`api_key_file` / `ngrok_domain_file` 引用；维护窗口要临时复用当前固定 public
endpoint 时，由 host-admin activate 脚本显式写入路径引用（见 §10）。

## 4. 任务 cwd 守卫

`/start`（及带 `cwd` 的 `/continue`）接受任务工作区前，先 canonicalize 真实路径
（symlink 先解析），`local` / `hpc` 一律拒绝：

- `$HOME` 及其任何祖先（含 `/`）；
- 本 Bridge 仓库根、其任何祖先、或仓库内任意路径；
- 当前实例状态根、其任何祖先、或状态根内任意路径；
- 当前实例 CODEX_HOME、其任何祖先、或 CODEX_HOME 内任意路径。

正常兄弟项目（如 `$HOME/Desktop/some-project`）只要不同时包含 / 位于上述路径
之内，均可正常使用。拒绝时返回结构化 `TaskCwdError`（通用 reason + category，
不泄露私有路径）。

目标：任务不能选 `$HOME` 或本 Bridge 仓库当"宽工作区"，从而改写控制面文件。
Bridge 维护（改实例、迁移、重启）是显式的主机管理（admin）工作流，不通过任务
面放行。

**maintenance 是反向规则**（`validate_maintenance_cwd`，`BridgeCore.start` 按
`build_cwd_guard` 的 `scope` 选择校验器）：维护窗口的任务工作区就是 Bridge
仓库本身——只接受仓库根或真实子目录（realpath 后判定，symlink 不能逃逸）；
`/`、`$HOME`、仓库祖先、仓库外项目、maintenance 实例状态根与 maintenance
CODEX_HOME 一律拒绝。`/continue` 不接受 `cwd`，不会扩权。local/hpc 的守卫
一字不弱化。

## 5. 管理命令（唯一写入者）

`scripts/bridge_instance.sh` 是实例状态的**唯一写入者**；`bridge_instance_lib.sh`
与 start/status/stop 等任务脚本只读。

```bash
./scripts/bridge_instance.sh list
./scripts/bridge_instance.sh show local
./scripts/bridge_instance.sh create local --template local   # 已存在则拒绝
./scripts/bridge_instance.sh create hpc --template hpc
./scripts/bridge_instance.sh create maintenance --template maintenance
./scripts/bridge_instance.sh update hpc port=8322
./scripts/bridge_instance.sh verify maintenance
./scripts/bridge_instance.sh migrate-current --dry-run
./scripts/bridge_instance.sh migrate-current --apply
```

`update` / `migrate-current --apply` 先写同目录备份（600）再落盘；`create` 会
创建实例目录与 runtime 目录（700）。配置校验：`mode` 只接受该实例模板允许的值
（hpc 永远不是 `danger-full-access`）、`approval_policy` / `network_access` /
绝对路径 / port 范围 / 引用文件存在 / 实例间碰撞。

## 6. 迁移：legacy 单例 → `local`

```bash
# 1) 预览（不写任何文件）
./scripts/bridge_instance.sh migrate-current --dry-run

# 2) 确认后落盘（备份 + 写入 local 实例；不复制/不打印 secret）
./scripts/bridge_instance.sh migrate-current --apply

# 3) 校验
./scripts/bridge_instance.sh verify local

# 4) 重启使实例生效（普通 Terminal；不重启运行中的 Bridge 不会自动生效）
./scripts/stop_ngrok_bridge.sh
./scripts/start_ngrok_bridge.sh          # BRIDGE_INSTANCE 默认 local，读实例配置
```

迁移只迁移非 secret 设置（模式、审批、网络、CODEX_HOME、port、runtime，以及
`.bridge_api_key` / `.ngrok_domain` 的**路径引用**）；legacy 文件
（`.bridge_sandbox_mode`、`.bridge_api_key`、`.ngrok_domain`）保持不动。

**legacy 回退（deprecation）**：实例配置不存在时，start / status / stop 回退到
旧单例行为（仓库根 `.runtime/`、默认 `workspace-write`）并打印 warning。已安装的
legacy LaunchAgent（`com.local.codex-bridge`）在迁移前可继续工作。

## 7. 并发与碰撞规则

- `local` / `hpc` / `maintenance` 同时运行的前提：**port 与 runtime_dir 都互
  不相同**（默认 8321 / 8322 / 8323 天然错开）。
- 任何碰撞（同 port 或同 runtime_dir）都 fail-closed：start / stop / status /
  install / verify 全部拒绝，绝不静默覆盖或共享 pid 文件。
- 每个实例使用自己的 `codex_home`，app-server 配置互不共享。

### 按实例的 LaunchAgent

```bash
./scripts/install_runtime.sh --instance local        # 先装稳定 runtime（非 Desktop）
./scripts/install_launch_agent.sh --instance local   # com.local.codex-bridge.local（supervisor 代理）
./scripts/status_launch_agent.sh --instance hpc
./scripts/uninstall_launch_agent.sh --instance hpc --stop
```

**自动托管只属于 local**：`com.local.codex-bridge.local` 的 ProgramArguments
指向稳定 runtime 的 `current/scripts/run_local_supervisor.sh --instance
local`（非 Desktop 路径），RunAtLoad=true，KeepAlive 用 `PathState` 绑定实例
状态 runtime 下绝对路径 `supervisor.enabled`（哨兵在才存活），
ThrottleInterval=10；plist 不含 secret/domain。port / runtime / CODEX_HOME /
approval / network 全部从实例配置派生（拒绝再传 `--sandbox-mode` /
`--network-access`）。hpc / maintenance **拒绝自动托管**（按需手动
`BRIDGE_INSTANCE=... start/stop_ngrok_bridge.sh`）；安装 local 代理前验证
runtime marker（`.runtime-build-info`/`runtime.manifest` + supervisor 脚本），
自动迁移/备份 legacy `com.local.codex-bridge` 或旧 Desktop plist（精确
bootout 指定 label + `.bak-<ts>`，不 pkill/killall、无 broad launchctl）。
不带 `--instance` 的安装保持 legacy `com.local.codex-bridge` 不变。卸载 local
代理会一并移除哨兵。

**维护窗口 pause marker**：`<state>/local/pause.marker` 是 maintenance 窗口的
保持信号。存在时前台 supervisor 停掉 local children 并等待（不重启、不退出，
哨兵保持 → launchd 无 crash-loop），marker 移除后自动恢复；`status_launch_agent.sh`
在 pause 中会显示 `supervisor: PAUSED`。

## 8. 回滚

- **配置回滚**：`update` / `migrate-current --apply` 每次先写
  `<instance>/backups/instance.conf.<timestamp>`（600）。回滚 = stop Bridge →
  `cp backups/... <instance>/instance.conf` → `verify` → start。
- **实例回退 legacy**：删除 `<state>/<instance>/instance.conf` 后，start/status/
  stop 回到 legacy 单例行为（打印 warning）。legacy 文件从未被迁移修改，因此
  随时可回退。
- **已安装 LaunchAgent**：1.1.0 的安装/迁移不会修改已安装的
  `com.local.codex-bridge`；卸载实例 agent 用
  `./scripts/uninstall_launch_agent.sh --instance <name> [--stop]`。

## 9. 维护纪律

- 实例状态只由 `scripts/bridge_instance.sh` 写入；任务脚本、测试、迁移不得写
  实例状态或 `.bridge_sandbox_mode`（回归扫描测试强制）。
- 不要从 Bridge 沙箱内部发起重启（2026-08-12 实测：沙箱内任何进程都继承
  seatbelt profile）；实例变更后请在普通 Terminal 执行 stop / start。
- 任何实例模板都不包含 secret；不要把 API key 或 ngrok 域名写进实例配置。

## 10. maintenance 维护窗口（HOST-ADMIN）

`maintenance` 是显式 host-admin 维护窗口：进入 / 离开只能由主机管理员执行
`scripts/activate_maintenance_instance.sh` / `scripts/deactivate_maintenance_instance.sh`，
普通任务 API / helper 没有切换入口。

activate（fail-closed，不使用 pkill/killall）：

1. 校验 local 实例存在且 `verify local` 通过；maintenance 已在运行则拒绝；
2. 准备 `~/.codex-deepseek-maintenance`：不存在时**只复制**
   `~/.codex-deepseek/config.toml`（绝不复制 threads/history/cache），目录
   `700`、config `600`；
3. 用 `scripts/migrate_codex_home_permissions.py --codex-home ...` 对 maintenance
   config 做与 bridge-workspace 相同的 legacy-sandbox/profile verify/migration
   （显式 CODEX_HOME 兼容参数，不硬编码路径）；
4. create（如缺）/ verify maintenance 实例（port `8323`、独立 runtime）；
5. **显式**把 `.bridge_api_key` / `.ngrok_domain` 作为**路径引用**写入
   maintenance 配置——只引用路径，绝不复制/打印 secret/域名内容；config-root
   稳定副本存在时优先引用它们（runtime 安装后），repo 路径引用是回退；这不
   是 maintenance 默认模板行为（`create maintenance` 两个引用均为空）；
6. supervisor 交接：记录 local supervisor 原状态（enabled|disabled）到
   `activate.marker`（instance state），**arm fail-safe rollback**（自交接点
   起任意失败都会回滚），在停 local **之前**创建 pause marker
   （`<state>/local/pause.marker`；哨兵保持，前台 supervisor 存活并停住 local
   children，launchd 无 crash-loop），再停 `BRIDGE_INSTANCE=local`（settle
   等待 supervisor 遵守 marker）；此后任意失败（ERR trap 或显式 die）都会先
   回滚再退出——maintenance 实例已配置时用 `stop_ngrok_bridge.sh`
   （`BRIDGE_INSTANCE=maintenance`，仅 managed 进程）安全停止，先清 pause
   marker，再恢复进入窗口前的 supervisor 状态（enabled → 哨兵 + launchd
   kickstart，否则 legacy start；disabled → local 保持停止），验证 local
   health + identity，public domain 可用时验证 public health；回滚日志绝不
   打印 API key/域名内容，原始失败原因保留且最终退出码仍非零；
   **成功进入窗口后 pause marker 保留**；
7. 再起 `BRIDGE_INSTANCE=maintenance`；
8. 验证 local/public health、`instance=maintenance`、`mode=bridge-workspace`、
   `port=8323`（`/health` 现在报告 `instance` / `mode` / `port`）；local health +
   identity + public health **全部通过后 disarm rollback**。

**LaunchAgent 注意事项**：local supervisor 代理的 KeepAlive 绑定哨兵；维护
窗口期间哨兵保持（supervisor 存活但被 pause marker 停住），launchd 不会
crash-loop。窗口期间**不要手工删除 pause marker 或 kickstart-restart local
的 LaunchAgent**（手工恢复会把 local 重新拉起在 8321 并与 maintenance 争用
固定 ngrok 域名）。

deactivate：停 maintenance → 清 pause marker → 读 `activate.marker` 恢复
local——原先 enabled 且 runtime agent 已装：**优先让 launchd supervisor
恢复**（已在运行则自行重启 children，否则哨兵 + kickstart）+ 验证
local/public health；agent 未装/不可用时 fallback 到 legacy start 流程；
原先 disabled：清 marker 后 local 保持停止；无 marker 的旧窗口按 enabled
处理（legacy 恢复）。**不删除** maintenance state 与
`~/.codex-deepseek-maintenance`（下次维护直接复用）；不碰 hpc / Para/Japan /
远端 jobs。

## 11. 运行时自动恢复（1.1.0）

**布局**（全部非 Desktop）：

- runtime：`${XDG_DATA_HOME:-$HOME/.local/share}/local-codex-bridge/`
  （`releases/release-<ts>-<head12>/` + `current` 原子符号链接 + 无 secret
  manifest）；只含运行必需 allowlisted tracked 文件，无 `.git`/tests/docs/
  backups/logs/secret/domain；保留最近 2 个 release（严格路径守卫清理）。
- state：`${XDG_STATE_HOME:-$HOME/.local/state}/local-codex-bridge/<instance>/`
  （实例配置 + `runtime/`：bridge/ngrok/supervisor pid 与日志、
  `supervisor.enabled` 哨兵、`activate.marker`）。
- config：`${XDG_CONFIG_HOME:-$HOME/.config}/local-codex-bridge/`（`api_key` /
  `ngrok_domain` 稳定副本，目录 `700`、文件 `600`；install 时从 repo/Desktop
  迁移并更新实例路径引用，内容不打印、原文件不删）。

**恢复矩阵**：

| 故障 | 恢复者 |
| --- | --- |
| ngrok 网络闪断 | ngrok 客户端自行重连（supervisor 不按健康杀进程） |
| bridge/ngrok 真退出 | supervisor 退避/节流补起（指数退避封顶 60s） |
| supervisor 崩溃 / logout / reboot | launchd `com.local.codex-bridge.local`（RunAtLoad + KeepAlive PathState 哨兵，ThrottleInterval=10） |
| 维护窗口 | 哨兵临时移除（activate 记录原状态，deactivate 恢复） |

**部署**（一次性 host-admin，需普通 Terminal）：`install_runtime.sh --instance
local` → `install_launch_agent.sh --instance local` → `status_launch_agent.sh
--instance local`。**launchd 实机装载未在 maintenance 沙箱会话执行**（
`~/.local/share` 写权限在沙箱外）；实现与 temp-dir 离线测试已交付，8 项
supervisor live 测试需 `ps`（CI/普通终端运行）。
