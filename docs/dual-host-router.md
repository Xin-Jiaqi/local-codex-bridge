# dual-host-router：一 GPT 双机 + Desktop OpenAI / Bridge DeepSeek 隔离

> 分支 `dual-host-router`（基于 `windows-bootstrap` @ `bf6300f`）的架构与运维
> 说明。公开 HTTP API、`openapi.yaml` 与 Mac 固定 ngrok URL **零改动**；所有
> dual-host 行为都是显式 opt-in（Mac 不配置 token 时与以前逐字节一致）。

## 1. 一句话总览

同一个 Custom GPT Action（同一个固定 URL、同一把 GPT API key）可以同时控制
Mac 和 Windows：Mac bridge 兼任"路由器"，Windows 不开放任何公网端口，只运行
一个**出站 worker** 主动连 Mac 领任务、在本机执行、回传结果。换机器 = 换
`/start` 的 `cwd`。

Windows 上的另一个问题是身份隔离：Desktop Codex 继续是 OpenAI（
`%USERPROFILE%\.codex`），只有 Bridge 用 DeepSeek（专用 CODEX_HOME），两者
通过目录分离实现，不依赖 `codex app-server --profile`。

```
ChatGPT (Custom GPT) ──(既有 Actions URL, 不变)──► Mac bridge 8321 (路由器)
                                                    │ 公开端点全部不变
                                                    │ 内部 /internal/worker/*（独立 token）
                                                    ▼
                                      Windows outbound worker（无公网端口）
                                                    │ http://127.0.0.1:8321
                                                    ▼
                                      Windows 本地 bridge（DeepSeek 专用 CODEX_HOME）
```

## 2. 路由规则

### /start 按 cwd 路由

`bridge/dual_host.py::classify_cwd`：

- Windows 原生路径 → `windows`：盘符绝对路径 `C:\...` / `C:/...`（含裸盘符
  `C:`）、UNC `\\server\share`、WSL 挂载 `\\wsl$\<distro>` /
  `\\wsl.localhost\<distro>`、正斜杠 UNC `//server/share`；
- macOS POSIX 路径、相对路径、**无 cwd** → `mac`（与 dual-host 之前完全一致，
  GPT Action 的默认无 cwd 调用继续落在 Mac）。

### 映射优先，缺失安全探测

- `ThreadTargetMap`（`thread_id → mac|windows`，JSON、原子写）持久化在实例状态
  `<state>/<instance>/thread_map.json`（或仓库 `.runtime/thread_map.json`；
  `BRIDGE_THREAD_MAP` 可覆盖）。只存 thread id + host，无 prompt/secret。
- `/continue /observe /steer /interrupt /read`：映射命中直接路由；映射缺失时
  先查本机、再以非变更性 `read` 探测 Windows（`probe` 等待 15s）；Windows
  离线（503/504）按"无此线程"回落到原 404 语义。探测只对线程路由做，绝不
  影响 Mac 本机线程。
- `/threads`：合并两端（本机立即 + Windows 有界 10s 等待）；Windows 离线时
  跳过远端、只列本机；两端同 id 去重，合并后按 `updated_at` 排序。

## 3. Windows 出站 worker（不开公网）

`bridge/worker.py`（`python -m bridge.worker`，纯 stdlib）向 Mac 路由器的
**内部 API** 长轮询：

| 内部端点 | 方法 | 说明 |
|---|---|---|
| `/internal/worker/poll` | POST | 长轮询取 job（`timeout_s` ≤30，默认 15），返回 `{"jobs":[...]}` |
| `/internal/worker/result` | POST | 回传 `{job_id, result}`（result 带 HTTP 形状 status/body/error） |
| `/internal/worker/status` | GET | worker 在线状态 / 队列统计（仅 token 持有者可看） |

安全不变量：

- **独立 token**：内部 API 用 `BRIDGE_WORKER_TOKEN`，≠ GPT 用的
  `.bridge_api_key`；`hmac.compare_digest` 比较。token 缺失 → 503
  `worker_api_disabled`；dual-host 未启用 → 404。
- **allowlist 端点**：kind 集合固定为 start/continue/observe/steer/interrupt/
  read/list；worker 本地的转发 URL 由 kind 静态推导（`KIND_ENDPOINTS`），
  路由器给什么 URL 都改不了它。
- **有界**：待办队列 ≤64、job TTL 300s、结果保留 ≤128 条、结果体 ≤512KB、
  poll ≤30s、router 侧远端等待有界（`REMOTE_OP_WAIT_S`）、worker 侧
  本地响应 ≤512KB + 读超时。
- **离线互不影响**：worker 指数退避重试；Windows 离线只让"明确指向
  Windows 的线程"请求 404/503，Mac 线程与列表不受影响。
- **日志卫生**：Mac router 与 worker 日志只含 thread/job id、kind、状态码，
  无 prompt、无 key/token。

worker 运行参数（ps1 默认注入）：`--router-url <Mac URL>` `--local-url
http://127.0.0.1:8321` `--local-api-key-file .bridge_api_key`
`--poll-timeout-s 15` `--state-file <runtime>/worker.state.json`；token 经 env
`BRIDGE_WORKER_TOKEN`，本地 bridge key 经 env `BRIDGE_API_KEY`（或参数文件）。

## 4. Mac 侧启用（一次性；公开链路不变）

1. 生成 gitignored token 文件（任意长随机串）：

   ```bash
   openssl rand -hex 32 > .bridge_worker_token && chmod 600 .bridge_worker_token
   ```

2. 重启一次 bridge（`./scripts/start_ngrok_bridge.sh` 检测到
   `.bridge_worker_token` 自动导出 `BRIDGE_DUAL_HOST=true` +
   `BRIDGE_WORKER_TOKEN`；等价于手动 export 后重启）。hpc/maintenance
   实例不读取该文件。

> ⚠️ 本次改造**不会替你执行这次重启**：当前 Mac 公网链路在运行，切换请挑
> 不断链的窗口由人工完成。重启前后公开 URL 与 API 完全一致，只有
> `/health` 多出 `dual_host` 状态字段。

## 5. Windows：Desktop OpenAI / Bridge DeepSeek 隔离

目录职责（全部由 `start_local_codex_bridge.ps1` 自动建立）：

| 目录 / 文件 | 用途 |
|---|---|
| `%USERPROFILE%\.codex` | Desktop Codex（OpenAI）。DeepSeek 标记时自动迁移/还原；`auth.json`、`state/`、`history/` 从不读取 |
| `%USERPROFILE%\.codex\backup-deepseek\config.toml` | 官方 OpenAI 备份，还原优先来源；缺失时写最小 `gpt-5.6-sol` OpenAI 配置 |
| `%LOCALAPPDATA%\local-codex-bridge\codex-deepseek` | 专用 DeepSeek CODEX_HOME（Bridge app-server 恒用它；config 迁移/生成，`env_key` 引用 key） |
| `%LOCALAPPDATA%\local-codex-bridge\secrets\deepseek.key.dpapi` | DeepSeek key（DPAPI CurrentUser 加密，不明文） |
| `%LOCALAPPDATA%\local-codex-bridge\secrets\worker.token.dpapi` | Mac 路由器 worker token（DPAPI） |
| `%LOCALAPPDATA%\local-codex-bridge\local\` | 实例状态：`instance.json`（无 secret）、`runtime/`（pid/日志/worker.state.json） |
| `scripts\windows\codex-deepseek.cmd` | CLI wrapper：临时设 `CODEX_HOME`=专用 DeepSeek profile 后调用真实 codex |

规则：

- Desktop 还原只在"Desktop config 被 DeepSeek 标记"或缺失时发生；已是普通
  OpenAI/自有配置则原样保留。
- 用户级 `OPENAI_BASE_URL` 仅当确认包含 `deepseek` 时清除；普通代理 /
  自建 base URL 保持原样（防误删）。
- DeepSeek key 与 worker token 只在子进程环境变量注入窗口存在，内存用后
  置空，任何日志/instance.json 都不含值。

## 6. Windows 一键启动（默认 dual-host 模式）

```powershell
powershell -ExecutionPolicy Bypass -File scripts\windows\start_local_codex_bridge.ps1
```

脚本自动完成：Python/Codex 检测与最小安装 → 配置隔离与迁移 → DPAPI 收
DeepSeek key → 起本地 bridge `127.0.0.1:8321` 并验证 `/health` + `/ready` →
安装 `codex-deepseek.cmd` → 起出站 worker 连 Mac（默认
`https://diploma-ideology-skier.ngrok-free.dev`；`-MacBridgeUrl` 或
`MAC_BRIDGE_URL` 覆盖；token 首次掩码输入 → DPAPI）。参数：

| 参数 | 含义 |
|---|---|
| （无） | dual-host 默认：本地 bridge + outbound worker；无 ngrok |
| `-MacBridgeUrl <url>` | Mac 路由器公网 URL |
| `-WorkerToken <token>` | 本次显式给 worker token（仍可 DPAPI 记忆） |
| `-NoNgrok` | 只起本地 bridge（调试/准备；旧参数名保留兼容） |
| `-NgrokCutover` | 回退旧"单机独占固定域名"模式（legacy） |
| `-Stop` | 停本脚本启动的 bridge/worker/ngrok |

## 7. 测试

- `tests/test_dual_host.py`（26）：cwd 分类、映射持久化/原子写、队列有界、
  TTL/结果保留、worker 转发桩、auth、日志卫生。
- `tests/test_dual_host_http.py`（12）：真实 HTTP handler + 真实 worker 循环
  （线程内 fake Mac/Win app-server，无网络）：`/start` cwd 路由、映射路由、
  worker auth/offline/timeout、`/threads` 合并。
- `tests/test_windows_support.py`（51：46 任意平台 + 5 Windows-only）：隔离
  迁移、DPAPI、worker 模式、wrapper、占位符、secret 面静态检查。
- CI：上述三文件加入离线集合；py_compile 覆盖 `bridge/dual_host.py` 与
  `bridge/worker.py`。真实 Windows 主机尚未实机验证（见 README 已知限制）。

## 8. 回退

- Mac：删掉 `.bridge_worker_token`（或不再 export）→ 重启 bridge 即完全回到
  dual-host 之前行为。
- Windows：`-Stop` 停掉即可；Desktop 已还原为 OpenAI；要回到旧
  "Windows 独占域名"模式用 `-NgrokCutover`。
