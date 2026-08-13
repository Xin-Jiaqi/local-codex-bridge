# Release 流程（v1.1.0）

> 目标：把"当前可复现的自动化验证"与"历史手动实测"分开写，避免 release notes
> 声称未复测的数字；已发布 tag 永远不移动、不重打。

## 发布前 checklist

1. **版本统一**：`bridge/__init__.py`、`openapi.yaml` `info.version`、app-server
   initialize `clientInfo`（`bridge/client.py`）与相关测试断言一致。检查命令：
   `rg -n '1\.[0-9]+\.[0-9]+' bridge/ http_server/ openapi.yaml tests/`，
   并确认 CHANGELOG 的上一版本数字只出现在历史条目中（1.0.1 从未发布，任何
   地方都不应声称它存在）。
2. **CHANGELOG**：新增明确的 `## [x.y.z]` 条目；unreleased 状态写
   `Unreleased`；发布时把状态行改为发布日期。**不改写已发布条目**。
3. **文档口径**：README「V1 状态」与 SECURITY「沙箱模式」明确区分
   "已发布版本" 与 "当前工作区/unreleased 行为"。
4. **离线测试**：`python3 tests/test_config_propagation.py tests/test_sandbox_mode.py
   tests/test_instance_isolation.py tests/test_workspace_guard.py
   tests/test_maintenance_instance.py tests/test_git_automation.py
   tests/test_migrate_codex_home_permissions.py tests/test_pid_guard.py
   tests/test_runtime_supervisor.py tests/test_activate_runtime_autorecovery.py
   tests/test_bootstrap_autorecovery_command.py`
   全绿（CI 跑同一 11 文件集合；runtime supervisor 的 11 项 live
   测试与 pid guard 的 3 项 live 测试需要 `ps`，CI/普通终端可跑，无 `ps` 的
   seatbelt 沙箱内跳过；git automation 的 1 项可选 sandbox 集成需要
   `RUN_SANDBOX_TESTS=1` 在普通终端跑。2026-08-13 沙箱实测口径：223 项 =
   208 passed / 15 skipped，逐文件结果见 `docs/release-validation-v1.1.0.md`）。
5. **静态检查**：`bash -n scripts/*.sh`、`py_compile`、`plutil -lint`、`git diff --check`。
6. **secret/privacy scan**（不输出命中值）：扫真实 ngrok 域名（对比
   `.ngrok_domain` 内容）、`/Users/` 个人路径、`sk-`/`ghp_`/`Bearer` token 模式、
   `git config user.email`、`.runtime/`/`*.pid` 内容、实例状态根
   （`~/.local/state/local-codex-bridge/`，确认不混入 diff）。`deliverables/`
   属外部仓库维护产物，已 gitignore，**必须排除不扫不提交**
   （`git status --short` 不得出现）。
7. **diff review**：确认无临时实验文件、迁移备份（`config.toml.bak`）、真实
   CODEX_HOME config、Obsidian/个人库内容混入；`deliverables/` 目录本身留在磁盘，
   不进 git。
8. **CI**：push 前 `.github/workflows/ci.yml` 必须已在 PR/本地跑绿。

## 一次性 host-admin runtime 部署（1.1.0 起，发布前或发布后均可）

launchd 自动恢复的稳定 runtime 装在 `~/.local/share`，不在任务沙箱写权限内，
必须由 host-admin 在普通 Terminal 执行一次（之后每次升级只需重跑 install）：

```bash
./scripts/install_runtime.sh --instance local       # runtime + 凭据路径引用迁移（保留最近 2 个 release；写 .runtime-build-info）
./scripts/install_launch_agent.sh --instance local  # per-instance 代理 + 哨兵 + legacy 迁移
./scripts/status_launch_agent.sh --instance local   # 复核 loaded/running/runtime/release/supervisor/pause
```

卸载：`./scripts/uninstall_launch_agent.sh --instance local --stop` 后
`./scripts/uninstall_runtime.sh`（默认保留 state/CODEX_HOME/config 凭据）。
恢复矩阵与哨兵语义见 `docs/instance-isolation.md` §11。注意：release 流程本身
不自动执行该部署；若 release notes 声称 launchd 已实机验证，必须先真跑过上面
三条命令并记录结果，否则只写"实现 + 离线测试已交付，实机装载待 host-admin"。

## 发布执行

```bash
# 1. 提交（单 commit，信息含版本号，例如 "release: Local Codex Bridge v1.1.0"）
git add -A && git diff --cached --check
git commit -m "release: Local Codex Bridge v1.1.0"

# 2. push main
git push origin main

# 3. 打 annotated tag（只指向该 release commit；tag 一经 push 永不移动/重打）
git tag -a v1.1.0 -m "Local Codex Bridge v1.1.0"
git push origin v1.1.0

# 4. GitHub Release（从 CHANGELOG 条目整理 notes，不新建重复 release）
gh release create v1.1.0 --title "Local Codex Bridge v1.1.0" --notes-file <notes>
# 若 release 已存在需修正：gh release edit v1.1.0 --notes-file <notes>
```

## Release notes 规则

- 只引用**当前可复现**的数字（离线单测/CI）；历史手动实测（如 2026-08-11
  core/actions/HTTP/tunnel 全 PASS）必须标注"历史实测、未在发布前复跑"。
- 不声称未验证的能力；bridge-workspace 的 root `.git` 写入与 GitHub 连通性
  只在普通 Terminal 的 `verify_bridge_git_automation.sh --network` 通过后写进 notes。
- 明确列 breaking/semver 说明：本仓库 minor 版本新增能力但保持 HTTP/Actions
  公共接口向后兼容；无 breaking change 不升 major。

## 内容稿 final sync（可选，不绑定软件 release）

- 知乎（`docs/zhihu-v1.md`）与小红书（`docs/xhs-v1.md`）有独立的发布节奏，
  不要求与 v1.1.0 软件 release 同一天；内容发布始终由用户人工完成。
- 若内容稿在软件 release 之后发布：先按 `docs/content-publishing-checklist.md`
  做一次 "unreleased / 当前工作区" 时点文字的 **final sync**（zhihu 改为按
  release notes 的事实描述；xhs 按稿顶「发布后同步点」注记执行），再配图、
  人工审阅、发布。
- 软件 release 不依赖内容稿是否已发布；内容稿发布也不阻塞软件 release。

## semver 口径（为什么 1.1.0 是 minor）

- HTTP/Actions 公共接口不变：无 breaking change、无新增功能 API；
  `openapi.yaml` 保持 8 个 operation、request/response shape、错误格式不变。
- 但部署 / 运维模型获得**新能力**：实例钉扎控制面（`local` / `hpc` 独立
  CODEX_HOME / port / runtime）、任务 cwd 守卫、多实例 LaunchAgent 与
  `migrate-current`；同时默认安全边界更强（hpc 模板永不使用
  `danger-full-access`、`.git/hooks/` 只读）。
- 按 semver：向后兼容的新能力 + 更强默认值 = **minor (1.1.0)**；无 breaking
  change 不升 major，不把新增能力压成 patch。
- 1.0.1 从未发布：所有原 1.0.1 unreleased 内容（sandbox 模式、
  `bridge-workspace` profile、CODEX_HOME 迁移、PID 守卫等）统一归档进 1.1.0，
  不要在 release notes / CHANGELOG 中声称 1.0.1 存在。

## 发布后验证

```bash
# 普通 Terminal（真实 Seatbelt）
./scripts/verify_bridge_git_automation.sh --network
# 期望 RESULT profile_git_commit=OK / workspace_write_git_denied=OK / network_readonly=OK

# 本地从 tag 重新 checkout 后跑离线测试，确认 tag 内容与 main 一致
git checkout v1.1.0 && python3 tests/test_config_propagation.py
```

- 离线测试含逐模式 app-server 参数断言（`tests/test_git_automation.py`）：
  bridge-workspace 的 `thread/start` 必须携带
  `config.default_permissions="bridge-workspace"`，thread/turn 参数不得含 legacy
  `sandbox` / `sandboxPolicy`；legacy 两档不得携带 `default_permissions`。

## tag 纪律

- 已发布的 tag（含 `v1.0.0`）**不移动、不重打、不删除再建**；补丁一律走新版本号。
- `git tag -f` / `git push --force --tags` 在本仓库视为事故，需要完整说明。
