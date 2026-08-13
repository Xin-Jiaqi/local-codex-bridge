# Release Validation — v1.1.0

> 日期：2026-08-13 · 本文档只记录可复现的离线门禁与静态检查结果。
> Host runtime activation **仍未在实机执行**（见文末），本文档不声称
> launchd live verified。

## 1. Safe offline suite（10 文件，可复现命令）

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
  tests/test_activate_runtime_autorecovery.py; do
  python3 "$t"
done
```

同一命令集合即 `.github/workflows/ci.yml` 的 offline 步骤与
`docs/release-process.md` checklist 第 4 项；两个文件清单已对齐（10 文件）。

### 结果（2026-08-13，seatbelt 沙箱实测；全部文件 `unittest` OK、退出码 0）

| 文件 | 总数 | 跳过 | 通过 |
|---|---:|---:|---:|
| test_config_propagation.py | 3 | 0 | 3 |
| test_sandbox_mode.py | 7 | 0 | 7 |
| test_instance_isolation.py | 41 | 0 | 41 |
| test_workspace_guard.py | 19 | 0 | 19 |
| test_maintenance_instance.py | 40 | 0 | 40 |
| test_git_automation.py | 29 | 1 | 28 |
| test_migrate_codex_home_permissions.py | 17 | 0 | 17 |
| test_pid_guard.py | 10 | 3 | 7 |
| test_runtime_supervisor.py | 35 | 11 | 24 |
| test_activate_runtime_autorecovery.py | 11 | 0 | 11 |
| **合计** | **212** | **15** | **197** |

### 跳过分类（15 项，均不计入通过）

- 11 项 runtime supervisor live 测试（含 pause-resume 与 runtime-copy 运行）：
  需要 `ps` 做 PID identity 验证；seatbelt 沙箱无 `ps`，CI（ubuntu-latest）
  与普通终端可跑。
- 3 项 pid guard live-process 测试：同样依赖 `ps`。
- 1 项 git automation 可选 sandbox 集成：需要 `RUN_SANDBOX_TESTS=1` 在
  普通终端（Seatbelt）跑。

## 2. 静态门禁（2026-08-13）

| 检查 | 命令（可复现） | 结果 |
|---|---|---|
| shell 语法 | `for f in scripts/*.sh; do bash -n "$f"; done` | 18 个脚本全绿 |
| python 编译 | `PYTHONPYCACHEPREFIX=<tmp> python3 -m py_compile http_server/server.py bridge/*.py scripts/migrate_codex_home_permissions.py tests/test_*.py` | 全绿（临时 pyc 落 /tmp） |
| plist lint | `plutil -lint scripts/launch_agent/*.plist` | 2 个 plist 全绿 |
| OpenAPI | `info.version=1.1.0`；8 个 operation；全部 `x-openai-isConsequential: false` | 全绿 |
| CI YAML | `yaml.safe_load(.github/workflows/ci.yml)` + 文件清单与 release-process 对齐 | 全绿 |
| whitespace | `git diff --check` | 无输出 |
| true-secret scan | 逐字节比较 `.bridge_api_key` / `.ngrok_domain` / `.public_url` 内容与全部 tracked + untracked 文件及 `git diff` | 无命中（命中内容不输出） |

## 3. Host activation 状态（明确 PENDING）

- 未在真实主机执行 `./scripts/activate_runtime_autorecovery.sh`；
- 未完成：runtime 安装、per-instance LaunchAgent 装载、supervisor 实机运行、
  bridge crash-recovery 实机自测、公网 /health 验证；
- 因此 release notes / 本文档口径 = 「实现 + 离线测试已通过（212 = 197 + 15），
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
