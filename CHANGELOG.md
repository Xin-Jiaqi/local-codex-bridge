# Changelog

1.0.0 之前的条目根据本地源码与 2026-08-11 测试报告重建，日期为近似值；
仓库自 2026-08-12 起纳入 git 管理（v1.0.0，分支 `main`）。

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
