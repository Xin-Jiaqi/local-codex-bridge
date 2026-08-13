# Contributing

Local Codex Bridge 是一个单人维护、公开可用的个人工程仓库。欢迎 issue 与小型 PR；
在动手前请先读 README 与 SECURITY.md，理解项目的安全边界定位（个人单机使用，
不承诺多用户/审批流/审计能力）。

## 本地测试

安全离线测试（无需 app-server、无需 DeepSeek key、无需网络，当前可复现）：

```bash
python3 tests/test_config_propagation.py
python3 tests/test_sandbox_mode.py
python3 tests/test_instance_isolation.py
python3 tests/test_workspace_guard.py
python3 tests/test_git_automation.py
python3 tests/test_migrate_codex_home_permissions.py
python3 tests/test_pid_guard.py
```

可选集成验证（需要 macOS Seatbelt + 普通 Terminal，不在 CI 运行）：

```bash
RUN_SANDBOX_TESTS=1 python3 tests/test_git_automation.py
./scripts/verify_bridge_git_automation.sh --network
```

需要真实 app-server + DeepSeek key 的集成测试（`test_bridge_core.py` /
`test_bridge_actions.py` / `test_http_api.py`）只能本机手动跑，**不要**把它们加入 CI。

改动后至少跑：

```bash
for f in scripts/*.sh; do bash -n "$f"; done
python3 -m py_compile http_server/server.py bridge/*.py tests/*.py
plutil -lint scripts/launch_agent/com.local.codex-bridge.plist   # macOS
git diff --check
```

## 代码风格

- 运行时零第三方依赖：Python 只用标准库；shell 用 bash（`set -euo pipefail`）。
- Python 遵循 PEP 8，命名自解释；新增逻辑必须配离线单测。
- 配置/模板/文档中文表述与仓库现有风格一致；英文术语保留原文。
- 版本字段（`bridge.__init__.__version__`、`openapi.yaml`、app-server
  `clientInfo`）必须同步，加 `tests/test_http_api.py` 里的版本断言一起更新。
- 实例 schema（`scripts/bridge_instance_lib.sh` 的 `bridge_instance_keys`）与
  `tests/test_instance_isolation.py` 的 `SCHEMA_KEYS` 必须同步；新增 key 只能
  是非 secret 字段，任何 secret 值不得进入实例配置。

## Secret 与隐私规则（强制）

- 永不提交：`.bridge_api_key`、`.public_url`、`.ngrok_domain`、
  `.bridge_sandbox_mode`、`openapi.ngrok.yaml`、`.runtime/`、`*.pid`、`*.log`、
  实例状态根（`~/.local/state/local-codex-bridge/`，含 `instance.conf` 与备份）。
- 代码/文档中不得出现：真实 ngrok 固定域名、API key/token、个人绝对路径
  （如 `/Users/<name>/...`，一律用 `$HOME`/`~`）、真实 PID、shell history、
  本机 `~/.codex*/config.toml` 内容或其备份。
- 打印/日志输出前先过一遍：不要 echo 配置值；迁移类脚本只允许打印键名。
- 迁移脚本 `scripts/migrate_codex_home_permissions.py` 的输出必须保持
  "不打印任何值"（含 API key），改动它时必须更新对应测试。

## 部署相关文件

以下文件属于本机部署产物或其他仓库的维护产物，不是项目内容：`config.toml.bak`、
`$CODEX_HOME` 下的一切、`~/.codex*/`、`deliverables/`（已 gitignore，保留在磁盘但
**永不提交**，扫描时明确排除）、本机生成的 `openapi.ngrok.yaml`、LaunchAgents
里的生成 plist、实例状态根（`~/.local/state/local-codex-bridge/`）。PR 中不得包含
这些内容，也不得包含 Obsidian 笔记或任何个人库内容。

## PR checklist

- [ ] 离线单测全绿（7 个文件，125 项）；新增行为有测试
- [ ] `bash -n` / `py_compile` / plist lint / `git diff --check` 通过
- [ ] secret/privacy scan 干净（`rg` 扫真实域名、`/Users/`、token 模式、PID；
      `deliverables/` 与实例状态根已 gitignore，扫描时排除）
- [ ] 没有部署产物、备份、真实 CODEX_HOME config、Obsidian 内容混入 diff
      （`git status --short` 不应出现 `deliverables/`）
- [ ] README/SECURITY/CHANGELOG 与行为一致；版本字段同步（若涉及）
- [ ] 不移动已发布 tag；不把 unreleased 行为写成已发布
