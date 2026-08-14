# Release Validation — v1.1.0

> 日期：2026-08-14（2026-08-13 基线 + 2026-08-14 维护期集中修复复跑）·
> 本文档只记录可复现的离线门禁与静态检查结果。
> Host runtime activation **仍未在实机执行**（见文末），本文档不声称
> launchd live verified。

## 1. Safe offline suite（12 文件，可复现命令）

```bash
for t in \
  tests/test_config_propagation.py \
  tests/test_sandbox_mode.py \
  tests/test_instance_isolation.py \
  tests/test_workspace_guard.py \
  tests/test_maintenance_instance.py \
  tests/test_git_automation.py \
  tests/test_migrate_codex_home_permissions.py \
  tests/test_pid_guard.py \
  tests/test_runtime_supervisor.py \
  tests/test_activate_runtime_autorecovery.py \
  tests/test_bootstrap_autorecovery_command.py \
  tests/test_host_ops_lock.py; do
  python3 "$t"
done
```

同一命令集合即 `.github/workflows/ci.yml` 的 offline 步骤与
`docs/release-process.md` checklist 第 4 项；两个文件清单已对齐（12 文件）。

### 结果（2026-08-14 维护期复跑；全部文件 `unittest` OK、退出码 0）

| 文件 | 总数 | 跳过 | 通过 |
|---|---:|---:|---:|
| test_config_propagation.py | 3 | 0 | 3 |
| test_sandbox_mode.py | 7 | 0 | 7 |
| test_instance_isolation.py | 41 | 0 | 41 |
| test_workspace_guard.py | 19 | 0 | 19 |
| test_maintenance_instance.py | 52 | 0 | 52 |
| test_git_automation.py | 29 | 1 | 28 |
| test_migrate_codex_home_permissions.py | 17 | 0 | 17 |
| test_pid_guard.py | 10 | 3 | 7 |
| test_runtime_supervisor.py | 43 | 0 | 43 |
| test_activate_runtime_autorecovery.py | 11 | 0 | 11 |
| test_bootstrap_autorecovery_command.py | 11 | 0 | 11 |
| test_host_ops_lock.py | 11 | 0 | 11 |
| **合计** | **254** | **4** | **250** |

### 跳过分类（4 项，均不计入通过）

- 3 项 pid guard live-process 测试：同样依赖 `ps`。
- 1 项 git automation 可选 sandbox 集成：需要 `RUN_SANDBOX_TESTS=1` 在
  普通终端（Seatbelt）跑。

> 注：2026-08-13 基线的 11 项 runtime supervisor live 测试（pause-resume、
> runtime-copy 运行）依赖 `ps`，在当时的 seatbelt 沙箱内跳过；本次复跑环境
> 有 `ps`，43 项全部执行通过。

## 2. 静态门禁（2026-08-14）

| 检查 | 命令（可复现） | 结果 |
|---|---|---|
| shell 语法 | `for f in scripts/*.sh; do bash -n "$f"; done` + `bash -n scripts/bootstrap_autorecovery.command` | 20 个脚本 + `.command` 全绿 |
| python 编译 | `PYTHONPYCACHEPREFIX=<tmp> python3 -m py_compile http_server/server.py bridge/*.py scripts/migrate_codex_home_permissions.py tests/test_*.py` | 全绿（临时 pyc 落 /tmp） |
| plist lint | `plutil -lint scripts/launch_agent/*.plist` | 2 个 plist 全绿 |
| OpenAPI | `info.version=1.1.0`；9 个 operation（含 `/ready`）；全部 `x-openai-isConsequential: false`；`/health` + `/ready` 均无 auth | 全绿 |
| CI YAML | `yaml.safe_load(.github/workflows/ci.yml)` + 12 文件清单与 release-process / release-validation 对齐 | 全绿 |
| whitespace | `git diff --check` | 无输出 |
| true-secret scan | 逐字节比较 `.bridge_api_key` / `.ngrok_domain` / `.public_url` 内容与全部 tracked + 非忽略 untracked 文件（350 个）及 `git diff`（gitignored 部署产物如 `openapi.ngrok.yaml` 按定义不可能进入 repo/diff，不计入） | 无命中（命中内容不输出） |

## 2b. 维护期集中修复（2026-08-14）

本次 maintenance instance 内完成一次控制面集中修复，全部离线验证：

1. **`/health` 500 回归**：`_BridgeHTTPServer` 缺 `_config_overrides` 时
   readiness 读取持续 500。已正式保留
   `self.httpd._config_overrides = self._config_overrides` wiring，并新增
   真实 `BridgeHttpServer` 回归测试（断言 `provider_config_ok`，不使用 fake
   server 掩盖）；`/health` + `/ready` 统一为真实 readiness gate。
2. **stable runtime 漏 `config/`**：`install_runtime.sh` allowlist 纳入
   `config/`；新增 installed-runtime dependency 测试——临时安装后必须包含
   `config/bridge-workspace.example.toml` 且仍不含 `.git`/tests/docs/secrets。
3. **deactivate 无 fail-safe**：`deactivate_maintenance_instance.sh` 自
   maintenance stop 起武装 fail-safe rollback，local 恢复/本地 health/
   identity/公网 health 任一步失败自动重开 maintenance 窗口并验证
   maintenance/bridge-workspace/8323 + 公网 health；仅 rollback 也失败才报
   DOUBLE FAILURE。新增 8 项动态回归（成功、local start 失败、本地 health
   失败、公网 health 失败、双重故障），全部 tmp/fake、不真 launchctl。
4. **single-writer host-ops lock**：`scripts/host_ops_lock_lib.sh`，state
   root 固定 `host-ops.lock` 目录 + `mkdir` 原子获取；记录 pid/operation/
   token/epoch（无 secret）；同 token 重入；并发 BUSY；owner pid 已死一次
   stale cleanup；EXIT trap 释放。接入 activate/deactivate maintenance、
   activate_runtime_autorecovery、bootstrap_autorecovery.command。新增
   11 项 lock 测试。普通 task API 不获得任何 host-op 能力。
5. **supervisor 误用不存在的 `/ready`（实机暴露）**：local supervisor
   轮询 `http://127.0.0.1:8321/ready` 返回 404，把本已成功启动且
   local/public health 曾 OK 的 stack 判为 not ready，触发 recovery/
   backoff。修复：readiness 只走 `/health` 并严格解析 JSON（`status=ok`、
   `ready=true`、`instance=local`、`mode=bridge-workspace`、
   `port=8321`），HTTP 200 本身不作为健康；共享 `bridge_health_ready`
   （supervisor_control 状态）同步改走 `/health`；fake curl 对 `/ready`
   返回 404（实机行为），supervisor 代码/测试不再依赖 `/ready`。新增 4 项
   supervisor 回归（healthy `/health` 不触发 recovery、HTTP 200 但
   `ready=false` 触发、错误 identity 触发、静态禁止 `/ready`）。
6. **deactivate public handoff / rollback race（实机暴露）**：local health
   identity 已 OK 但 public health 未及时 OK，rollback 被过早判 DOUBLE
   FAILURE。修复：local health ready 后对 fixed public endpoint 用 bounded
   propagation polling（默认 45s/3s，env 可覆盖，绝不无限重试）；public
   失败 rollback 前先重建 pause marker 并显式停 local managed children，
   再启动 maintenance；rollback 自身 local health + public 各用 bounded
   budget（默认 20s + 40s），真正超时才 DOUBLE FAILURE。新增/改写 3 项
   deactivate 动态回归（public 延迟数秒后成功不 rollback；public 失败 →
   pause + 停 local → maintenance rollback 且 rollback public 延迟后成功；
   真正超时才 DOUBLE FAILURE），全部 tmp/fake。

## 3. Host activation 状态（明确 PENDING）

- 未在真实主机执行 `./scripts/activate_runtime_autorecovery.sh`；
- 未完成：runtime 安装、per-instance LaunchAgent 装载、supervisor 实机运行、
  bridge crash-recovery 实机自测、公网 /health 验证；
- 因此 release notes / 本文档口径 = 「实现 + 离线测试已通过（248 = 244 + 4），
  实机装载待 host-admin」；不声称 launchd live verified。
- 实机执行前置条件：maintenance window ACTIVE（`activate_maintenance_instance.sh`
  成功且 local+public /health = maintenance/bridge-workspace/8323），普通 Terminal
  执行上面那一条命令；失败会打印阶段名并非零退出，不打印 domain/secret。

### Prior live maintenance verification（2026-08-13，operator 当天实际输出记录）

以下记录只覆盖 maintenance 窗口切换与 health identity，不等同于
1.1.0 runtime/supervisor/launchd 的实机装载验证：

- 2026-08-13 在实机完整执行一轮 maintenance activate → deactivate；
- maintenance 窗口内 local + public /health 均返回
  `instance=maintenance`、`mode=bridge-workspace`、`port=8323`；
- deactivate 后 local + public /health 均返回 200，
  `instance=local`、`mode=bridge-workspace`、`port=8321`。

该记录来自 operator 当天实际输出，不写真实 domain 值；host activation
（runtime 安装、LaunchAgent 装载、supervisor 实机运行）仍为 PENDING。
