# 在泳池边发语音，家里 Mac 自己动手：Local Codex Bridge 记录

上周末我做了件以前做不到的事。人坐在泳池边，手机开着 ChatGPT，用语音把手里一个任务的思路讲给模型听。任务不大，改一个脚本的循环逻辑，再跑一遍测试，但我特意没有坐在电脑前。说完我就下水了，游了一千米。上岸擦干头发，再拿起手机，家里那台 Mac 已经把活干完：文件改了，测试跑了，结果按我要求整理好了，都在对话里等着我。我只需要看结论，有问题再追问一句。

那台 Mac 不在我手边，在家里的书桌上。它干的活不在云上，在我自己机器上的 Codex 里。iPhone 上的 GPT 负责拆任务、盯进度，Mac 上的 Codex 负责执行，模型调用走我自己配的 DeepSeek。连接这层的，就是这篇文章的主角，Local Codex Bridge。

【图：朋友圈里我对这套桥的最初描述】

## 我为什么会想做这件事

这套流程以前是断开的。在手机上跟 GPT 把方案聊清楚，把结论整理成一段 prompt，复制到电脑终端里跑 Codex，再把输出复制回手机对话。任务本身五分钟，搬运和对照差不多也要五分钟，轮次一多，贴错版本的事情发生过；长输出在终端里滚动，哪一段对应哪个结论，要自己再对一遍。等待也是问题，模型生成到一半我插不上话，也没法让它先做别的。最隐蔽的损耗是上下文：每一轮搬运都是一次压缩，丢得最多的是约束条件，哪个文件不能动、哪种命名风格要遵守、哪一步已经确认过。这些细节在对话里明明说过，搬运一轮之后就没了，得重新交代一遍。

【图：一句自然语言让 ChatGPT 调用本地 Codex 的实际效果】

## 手机上的 GPT，家里 Mac 上的 Codex

现在这套东西跑起来以后，手机像一个远程项目经理的界面。GPT 在对话里把任务想清楚、拆步骤、检查结果，需要图的时候直接生成或整理好，多个任务统筹着推进；Codex 在本地工作区改文件、跑命令、做验证。DeepSeek 出模型调用，密钥和工作区都在我的机器上。Mac 要做的只有一件事，开着机、联网。人在不在电脑前不重要，在外面用手机发一句话，任务都照常推进。同一个任务在同一个 native thread 上继续，模型记得前文，不用重新贴历史。成本上，模型调用走 DeepSeek，比让云端全程跑一个 Agent 便宜，执行用的还是自己的机器。

具体到我的用法，现在常发生的是这样：我在手机上说一句，把当前仓库的 TODO 按文件整理成清单，GPT 调 /start 在本地建一个 thread 跑起来，observe 等结果，拿到清单后问我下一步。中途我想改方向，就补一句，它调 steer 把新指令排进当前这轮之后；想停，interrupt 立刻打断。整个过程我不在电脑前，对话记录留在手机里，随时能翻。

手机这个界面的价值在于，它是为我服务的对话层。GPT 会把结果整理成人话，把失败原因讲清楚，把下一步的选择列出来。Codex 在本地做的那些原始输出，不用我逐个看。需要图的时候，GPT 在对话里生成示意图，或者把数据画成图，配着解释一起给我。v1.1.0 里开机自动拉起 Bridge 的 runtime、supervisor 和 launchd 代理已经写完，254 项离线测试全绿（250 通过、4 跳过）；2026-08-14 在真实 host 上完整跑过 round-trip，自动恢复实机验证通过，v1.1.0 随后发布（发布状态待最终核对）。旧版 Desktop 代理有 TCC 权限问题，运行时迁出 Desktop 正是这次改动的一部分。

在外面用手机交代任务，对我来说是常态。语音输入的体验比打字顺，思路讲一半可以随时打断补充；iPhone 上的对话是我唯一要盯的界面，结果、失败原因、下一步选择都在那里。任务粒度也从一次改一个文件，慢慢变成一连串：整理清单、改代码、跑测试、写总结，中间没有需要我插手的环节。这套东西解决的是人在外面、活在家里的时差，任务不用等我到家才开工，我也不用为一次小改动专门坐到电脑前。

## 自动化以后，权限反而更需要收紧

自动化越深，权限越不能含糊。从公网进来的会话持有 key，就在本地工作区里有命令执行权，这个边界必须比平时更严。key 认证、工作区沙箱、任务目录守卫，三条线缺一条我都不会把公网口打开。

三条线可以展开说一下。key 认证决定谁能进来；工作区沙箱决定进来之后能碰什么；任务目录守卫决定能把哪个目录当工作区。app-server 以 on-request 的审批策略启动，Bridge 不把任何审批请求转发给 ChatGPT，工作区外的东西一律自动拒绝，没有升级通道。三条线都收敛在本地，公网只露出一个需要 key 的 HTTP 口，每条线都有对应的代码和测试兜底。

公网进来的会话能做的范围是有限的：指定一个普通项目目录当工作区，在工作区内读写文件和跑命令。$HOME、Bridge 仓库、实例状态目录都不在接受名单里，实例不能切，共享配置更不能改。

这个原则现在写进了文档，也写进了回归扫描测试：任务脚本、测试、迁移都不允许写实例状态和 .bridge_sandbox_mode。

这个认识不是一开始就有的。v1.0.0 的 Bridge 只有一个当前配置，任务工作区没有边界。有一次我同时挂着两个项目的会话，一个叫 Para，一个叫 Japan，它们共用同一份 Bridge 配置，一个会话把共享配置污染了，另一个会话的启动行为跟着变。事情本身不复杂，改回来就是，但它把问题摆得很清楚：任务面和控制面必须分开。1.1.0 的实例隔离就是从这个事故开始的。控制面状态现在放在任务工作区之外，进程启动时钉扎到 local、hpc、maintenance 三个实例之一，任务侧没有任何切换实例的接口。远程执行可以，越权不行。

如果你只是想知道这东西有什么用、我为什么会真的用它，看到这里基本就够了。接下来主要是实现细节，留给想自己复现、继续改，或者准备把仓库直接交给 Agent 的人。

## 如果你想自己复现，或者把它交给 Agent

技术栈刻意保持零第三方运行时依赖：Python 标准库 HTTP server 加手写 JSON-RPC 客户端，bash 脚本管后台进程，实测环境是 macOS arm64 和 codex 0.147.0。

**架构。** 一条完整的链路：

```text
ChatGPT（Custom GPT）
  → GPT Actions 调用（Bearer API Key，x-openai-isConsequential: false）
  → HTTPS → ngrok 固定域名 → 127.0.0.1:8321
  → Local Codex Bridge HTTP API（http_server/，Python 标准库 ThreadingHTTPServer）
  → JSON-RPC 2.0 over stdio
  → codex app-server（一个持久进程，model=deepseek-chat，model_provider=deepseek，model_reasoning_effort=max）
  → native thread / turn
  → DeepSeek（DEEPSEEK_API_KEY 只在本机）
  → 本地工作区（workspace-write 或 bridge-workspace，approval_policy=on-request）
```

【图：Local Codex Bridge 架构】

全链路只有一个持久 app-server 进程，Bridge 和它一对一连接。模型后端、API key、工作区全部在本地，公网只暴露一个带 key 的 HTTP 面。TLS 在 ngrok 这一侧终止，本机只监听 127.0.0.1。选 stdio 做本地进程间通信，公网面由桥自己提供，HTTP、认证、参数校验都收在 http_server 这一层。运行时代码只用 Python 标准库，没有 requirements.txt，部署少一步，长期后台跑的进程出问题的面也小。协议 schema 直接复用官方 schemas 目录的 v1/v2，没有自己发明格式，接口有变化时对着 schema 改，比对着日志猜稳。

**动作集。** 动作集不是一次设计出来的。0.1.0 时代只有 health、start、observe、continue 四个端点，steer、interrupt、list、read 是按真实使用中缺什么补什么加上去的，到 v1.0.0 才收敛。现在的八个端点：

- start：新建 native thread 并开始第一轮，可指定 cwd
- continue：同一 thread 继续，不复制历史
- observe：有界、事件驱动地等 turn 结束，单次最长 10 秒，不轮询
- steer：向运行中的 turn 排队注入新指令
- interrupt：中止运行中的 turn
- list：列出 native threads，带 cwd、preview、status、updated_at
- read：读取 thread 的真实 turn 历史，摘要化返回
- health：无认证的探活接口，返回 app-server 状态、model、model_provider、instance、mode、port

observe 的 10 秒上限是配合 GPT Actions 的超时约束选的，默认 5 秒。长任务的标准组合是 start、observe、再 observe、必要时 continue，不做一次长等待；超时返回 running，由调用方决定下一步。steer 在 Codex 0.147.0 里是排队语义，不打断当前生成，当前回复结束后才注入；它带 expectedTurnId 前置校验，服务端返回不同 turn 立即报错，绝不静默漂移。想立刻转向，用 interrupt 加 continue：interrupt 中止运行中的 turn 并返回最终状态，之后 continue 在同一个 thread 上接着做。

list 和 read 是给模型补上下文的。thread 列表带 cwd、preview、status 和更新时间，GPT 可以据此判断哪个任务还挂着；read 把真实 turn 历史摘要化取回对话，长任务做到一半，模型忘了前面决定过什么，就用它把关键结论捞回来。

OpenAPI 是 Custom GPT Actions 的菜单，8 个端点全部定义在里面，Custom GPT 导入这份文档，就知道有哪些动作、参数是什么、怎么认证。openapi.yaml 是公开模板，servers.url 是占位符；部署副本 openapi.ngrok.yaml 放真实域名，gitignored，不随仓库发布。所有 operation 都标了 x-openai-isConsequential: false，意思是 ChatGPT 调用前不弹确认。这个声明只是对 ChatGPT 的声明，安全边界由 key 认证、工作区沙箱和任务 cwd 守卫构成。

【图：Custom GPT Actions 配置】

client 的实现有两处容易出事的细节：请求按 id 和响应配对，通知分发到独立 handler，app-server 退出时所有 pending 请求统一以干净的错误失败；子进程的 stderr 由独立 reader 读走写日志，防止管道缓冲区堵住 app-server。握手失败直接启动失败并清理子进程，不留半启动状态。

启动时用 -c 把 model、model_provider、approval_policy 和沙箱边界显式传入，不依赖本机 config 文件，本机配置漂移影响不到远程会话。

CODEX_HOME 指向独立 profile（默认 ~/.codex-deepseek），DeepSeek provider 和 key 都在这个 profile 里，和日常终端的配置互不干扰；DEEPSEEK_API_KEY 只存在于启动 bridge 的 shell 环境。

生命周期由 bash 脚本管：start、stop、status 三个入口，运行时的 PID 和锁放在 .runtime 目录，stop 只停自己启动的进程，复用 PID 前先做只读校验，校验失败只报告 stale 进程，绝不 kill。开机自启的 launchd per-instance 代理、前台 supervisor 和 pause marker 都在 v1.1.0 里写好，254 项离线测试全绿（250 通过、4 跳过）；真实装载在 2026-08-14 的 host round-trip 里完成。开发中实测过一条运维规则：不要从 Bridge 沙箱内部发起重启，沙箱内拉起的任何进程都会继承 seatbelt profile，连 launchctl kickstart 拉起的 job 也一样；切换沙箱模式要在普通终端里 stop 再 start。

会话模型以 thread 和 turn 为句柄，thread_id 和 turn_id 是调用方唯一需要记住的两个值。continue 永远在同一个 native thread 上开新 turn，不复制历史、不重建上下文，这是模型记得前文的实现基础。thread/start 的参数固定，模型和 provider 不接受调用方覆盖，ChatGPT 不能把任务指到别的模型上。V1 没有做 MCP layer，当时的诉求就是把动作暴露成 HTTP，少一层就少一个调试环节。

**认证和请求边界。** 除 GET /health 外，所有端点要求 Authorization: Bearer，用 hmac.compare_digest 做常量时间比较。key 用 openssl rand -hex 32 生成，256-bit，chmod 600 存在 gitignored 的文件里，运行进程只通过环境变量拿它，从不写入脚本、日志或 pid 文件。轮换就是重新生成、重启、更新 ChatGPT 里的配置，重启之后旧 key 立即失效，认证每次请求实时比对。持有 key 的会话在工作区内有命令执行权，公网暴露时严格控制分发。health 无认证是有意设计，供隧道和探测用，不返回敏感信息。

威胁模型按最坏情况想：持有 key 的会话在工作区里有命令执行权，key 的分发范围就是信任范围。公网暴露时我控制分发，并定期轮换。当前版本没有内置限流、失败锁定和 IP 白名单，长期公网使用建议在隧道或网关层补上。

请求侧有明确的边界：

| 项 | 边界 |
|---|---|
| 请求体 | 上限 64KB，超出返回 413 |
| 字符串字段 | 最长 4000 |
| observe | wait_ms 默认 5 秒、上限 10 秒 |
| list | limit 夹在 1 到 20 |
| read | 最多取 20 轮 turn，assistant 文本截断到 4000 |

这些数字让远程会话不能把请求无限放大。

**隧道。** 公网面走 ngrok 固定域名，域名是个人 ngrok 账号资产，存在 gitignored 的 .ngrok_domain 文件里，不随仓库发布。start_ngrok_bridge.sh 启动时做三段验证：本地 /health、tunnel 上线、公网 /health，全绿才算 READY。脚本记录 PID，stop 只杀自己启动的进程，不碰别人的。

固定域名也是有意选的：OpenAPI 里的 servers.url 要稳定，Custom GPT Actions 的配置不用跟着隧道地址变。

换机器或换域名时，更新 .ngrok_domain 和 OpenAPI 部署副本，重启桥就行。

【图：Bridge + ngrok READY 输出】

**沙箱模式。** 默认的 workspace-write 下，工作区根目录的 .git 是 protected path，只读，网络默认关闭，需要时用 BRIDGE_NETWORK_ACCESS=true 打开。这是 Codex 0.147.0 的沙箱设计，不是 bug。我拿默认配置跑需要 git 的任务时才发现，Bridge 自己没法 git add 和 commit。

BRIDGE_SANDBOX_MODE 有三档。workspace-write 是默认，工作区内的读写和命令不需要确认，工作区外自动拒绝；bridge-workspace 是纯 permission profile，给需要 git 和 GitHub 的任务用；danger-full-access 和直接跑 CLI 的沙箱行为一致，没有限制，只靠显式选择启用，公网暴露时尤其危险。日常我跑的 local 实例用的是 bridge-workspace。两档的差别集中在 .git 和网络：workspace-write 下 .git 只读、默认无网络，bridge-workspace 放行 .git 元数据写入和 GitHub 白名单网络。

查 Codex 的 permission profiles 后确认了一条规则：beta profile（default_permissions）和 legacy 的 sandbox_mode、[sandbox_workspace_write] 不能混用，任何 loaded config 含 legacy 键，Codex 就退回旧沙箱并忽略 default_permissions。所以 bridge-workspace 是纯 profile：启动时通过 -c 注入 default_permissions 和 [permissions.bridge-workspace]，extends=":workspace" 保留 baseline protections，thread/start 的 config.default_permissions 指向这个 profile，legacy sandbox 字段保持缺席。server 在 bridge-workspace 下发现 legacy 键会拒绝启动，配套迁移脚本 migrate_codex_home_permissions.py 提供 dry-run、verify、apply 三步，只移除 legacy 键、保留其他配置逐字节不变，apply 前写 600 权限备份，输出只打印键名不打印值。

bridge-workspace 放开工作区根 .git 元数据写入和 GitHub 域名白名单网络：github.com、*.github.com、api.github.com、ssh.github.com、*.githubusercontent.com、objects.githubusercontent.com、raw.githubusercontent.com。.git/hooks/ 显式只读，阻止任务往 hooks 里投放可执行文件。当前 permission schema 只有 read override，没有 deny 语义，这个限制是按 schema 能力如实实现的。git 代理也做了隔离：bridge-workspace 下 app-server 子进程用 GIT_CONFIG_* 环境变量把 http.https://github.com.proxy 和 http.proxy 覆盖为空，丢弃代理相关环境变量，git 直连 GitHub，不依赖本机全局代理，~/.gitconfig 本身不动。

GitHub 白名单列了跑 git 和 GitHub API 需要的全部主机，但它不等于只能访问 GitHub 的强隔离，DNS 重绑定这类攻击面和 Codex 官方 network_proxy 的语义一致，这一点文档里写明了。

**实例隔离。** 控制面状态在任务工作区之外：${XDG_STATE_HOME:-$HOME/.local/state}/local-codex-bridge/<instance>/，目录 700，配置 600，update 和 migrate 每次先写带时间戳的备份再落盘。仓库里没有可切换的 profile 指针，BRIDGE_INSTANCE 在进程启动时钉扎，默认 local，运行中不可切换。实例配置只含非 secret 字段，API key 和 ngrok 域名只存路径引用。

这个设计是有前史的。第一版尝试在仓库里放一个可切换的 active_profile 指针，让任务自己选宽窄权限，安全审查把它判为 P0：任务侧存在切到更宽 profile 的切换面。整个机制废弃，不留任何可切换形态，改成进程启动时钉扎。

三个实例的定位：

- local：默认实例，bridge-workspace 加 GitHub 白名单网络，日常 coding 和 release 自动化，最小授权
- hpc：独立 CODEX_HOME（~/.codex-deepseek-hpc）、端口 8322、独立 runtime，workspace-write 加 on-request，模板永不使用 danger-full-access；没配 ngrok 域名就启动报错，不自动复用 local 的域名
- maintenance：显式 host-admin 维护窗口，独立 CODEX_HOME（~/.codex-deepseek-maintenance）、端口 8323，模板 bridge-workspace，永不使用 danger-full-access

hpc 的组合（workspace-write 加 on-request 加网络开启）写死在模板和测试里，为的是让远端运维会话永远碰不到 danger-full-access。Para 和 Japan 那次事故的对应物就是 hpc：它给独立远端运维用，和 local 各有各的 CODEX_HOME、端口、runtime，会话之间不再共享配置，也就不存在互相污染。maintenance 是给 Bridge 自己仓库维护用的窗口，进入和离开只能由主机管理员跑 activate_maintenance_instance.sh 和 deactivate_maintenance_instance.sh，普通任务 API 没有切换入口。activate 带 fail-safe 回滚：先停 local，arm 回滚，启动 maintenance，验证 local 和 public health 全部通过后 disarm；窗口内任务 cwd 只允许 Bridge 仓库本身或它的真实子目录。deactivate 停 maintenance 起 local，不删除 maintenance 状态，也不碰 hpc 和远端 jobs。

从 v1.0.0 的 legacy 单例迁移过来有现成路径：migrate-current 先 dry-run 预览，再 apply，apply 前写 600 权限的备份，legacy 文件本身不动；实例配置缺失时，start、status、stop 会回退到旧单例行为并打印 warning。local 的自动托管只走 com.local.codex-bridge.local 这一个 label，ProgramArguments 用 --instance local 显式钉住，port、runtime、CODEX_HOME 全部从实例配置派生，不接受额外参数覆盖；hpc 和 maintenance 是按需实例，永远不进自动托管，维护窗口期间 local 用 pause marker 停住（哨兵保持、supervisor 存活），退出窗口清 marker 恢复。

任务 cwd 有守卫。/start 接受路径前先 canonicalize 真实路径，symlink 先解析，拒绝 $HOME 及其祖先、Bridge 仓库、当前实例状态根、当前实例 CODEX_HOME；/continue 在 HTTP 层不接受 cwd 字段，thread 保持 start 时验证过的工作区，不存在扩权路径。拒绝时返回结构化 TaskCwdError，只给通用 reason 和 category，不泄露私有路径。三个实例同时运行的前提是 port 和 runtime 都互不相同，任何碰撞都 fail-closed，start、stop、status、install、verify 全部拒绝，绝不静默覆盖。

这次维护期还踩过一个并发所有权的坑。多个 ChatGPT 会话可以并行跑不同的任务，这是设计内的事；但 host 控制面（activate、deactivate、runtime 安装、自动恢复脚本）同一时刻只能有一个写者。并发控制以前靠习惯，两个会话同时操作同一个控制面就会互相踩。现在控制面操作统一过一把 single-writer lock：mkdir 原子获取，记录 pid 和操作名，同一操作可重入，其他并发操作直接 BUSY，owner 退出后释放。结论是任务面可以并行，控制面必须串行。

**这次踩的两个坑。** 要说明的是，这两个回归是这次 1.1.0 升级自己引入的，和运行环境无关。

一个是 /health 的身份字段。health 的 payload 加了 instance、mode、port，handler 从 self.server 上取这三个值，但 BridgeHttpServer 初始化时只挂了 core、api_key、log，忘了把实例身份挂到 httpd 上，真实进程里 /health 拿不到这三个字段。这个字段对 maintenance 窗口很关键，activate 脚本要拿它验证当前到底是哪个实例在服务。旧测试没抓到，原因是集成测试只断言 status、model、model_provider，不检查新增的身份字段，单元测试用的假 server 属性齐全，同样覆盖不到真实初始化的接线。现在补的回归测试直接构造真实 BridgeHttpServer，断言 httpd.instance、httpd.mode、httpd.port 以及 /health 的实际输出。

另一个是沙箱模式的环境变量。start 脚本把解析好的模式存在 SANDBOX_MODE 变量里，导出 BRIDGE_SANDBOX_MODE 时漏了从它赋值，子进程拿到的环境变量是原有的值或者空值，server 回退到默认 workspace-write。结果实例钉扎的模式（比如 maintenance 的 bridge-workspace）和 .bridge_sandbox_mode 文件都被静默忽略，边界比配置里写的宽。旧测试没抓到，原因是 config propagation 单测断言的是 Python 层 spawn 参数，bash 层测试断言的是实例配置解析，没有一条真正 source 启动脚本、检查导出给子进程的环境变量。现在补的回归测试 source 真实启动脚本，断言导出后的 BRIDGE_SANDBOX_MODE 等于实例配置里的模式。

两个 bug 的现场都留过痕迹。health 身份字段缺失那次，真实进程里 /health 一走到 instance、mode、port 就报错，假 server 的测试却全绿；沙箱模式那次，.bridge_sandbox_mode 文件里写的是 bridge-workspace，起起来的 server 却按 workspace-write 跑。修复都不大，各一行到三行，但这两处恰好说明：配置从文件到环境变量、从变量到子进程、从进程到 handler 的每一段传递，都要有测试压着。

这两处都是替身测试覆盖不到真实运行时接线的典型。所以这次补的回归测试专走真实路径：真实的 BridgeHttpServer 构造，真实的启动脚本环境传播。

两处修复都合进当前工作区之后，再跑一遍完整离线测试，口径就是下一节写的 250 passed、4 skipped；实机上 local 和 maintenance 的 health 也重新验证过。

**测试口径。** 当前工作区可复现的测试口径是 250 passed、4 skipped，这 4 项不计入通过。4 项 skipped 里，3 项是 pid guard 的 live-process 测试，需要 ps，在无 ps 的沙箱里跳过；1 项是 git automation 的可选 sandbox 集成，需要 RUN_SANDBOX_TESTS=1 在普通终端跑。离线测试总数 254 项，分布是：instance isolation 41 项、maintenance 52 项、git automation 29 项、runtime supervisor 43 项、workspace guard 19 项、migrate codex home 17 项、pid guard 10 项、sandbox mode 7 项、config propagation 3 项、activation autorecovery 11 项、bootstrap autorecovery 11 项、host ops lock 11 项。这 250 项在当前环境直接跑通。

集成测试需要真实 app-server 和 DeepSeek key，不进 CI。2026-08-11 的完整手动实测记录是 core 5/5、actions 7/7、HTTP API 11/11。当前 test_http_api.py 里有 12 个唯一场景，比历史记录多 1 项，新增的那项没有复跑确认。

集成测试的三个文件是 test_bridge_core.py、test_bridge_actions.py、test_http_api.py，按 README 手动跑，需要本机有 codex 和 DeepSeek 配置。

实机验证分两轮。2026-08-13 的维护期记录：maintenance 在实机完整跑过一轮 activate 和 deactivate，窗口内 local 和公网 /health 都返回 instance=maintenance、mode=bridge-workspace、port=8323；deactivate 之后 local 的本地和公网 /health 都是 200，返回 instance=local、mode=bridge-workspace、port=8321，覆盖 maintenance 窗口切换和 health identity。2026-08-14 的真实 host round-trip：stable runtime 安装、per-instance LaunchAgent 唯一 label 装载、supervisor 实机运行（pid 43975）、对 managed bridge pid 20930 真实 TERM 后由 supervisor 补起新 pid 21216 且 local+public health 恢复、ngrok 未被误杀、final status OK、随后自动重新进入 maintenance 并验证 maintenance/bridge-workspace/8323；结尾两个 marker 都是 YES。v1.1.0 随后发布（发布状态待最终核对）。

**版本时点。** v1.0.0 已经发布，仓库公开，BSD-3-Clause，tag 固定在 fa82e91、不移动。v1.1.0 在 2026-08-14 通过真实 host round-trip 后发布（发布状态待最终核对）：实例隔离、cwd 守卫、maintenance 维护窗口、bridge-workspace、运行时自动恢复（runtime + supervisor + per-instance LaunchAgent）、single-writer host-ops lock 和迁移脚本都在 1.1.0 线里，1.0.1 从未发布。文章里说的现在，都指这个时点。0.147.0 的 steer 排队、protected .git、profile 行为都未文档化或属于 beta，升级 Codex 前要重新验证。

【图：GitHub v1.1.0 Release 页面（发布后补）】

**自动恢复。** 这一版的运行时部署是显式两步：`install_runtime.sh --instance local` 把运行必需的 allowlisted 文件装进 `~/.local/share/local-codex-bridge/`（staging + 原子 current 符号链接，保留最近两个 release，写无 secret 的 `.runtime-build-info`），再把 local 的 key/domain 路径引用从 Desktop 迁到 `~/.config/local-codex-bridge/`（目录 700、文件 600，内容不打印、原文件不删）；然后 `install_launch_agent.sh --instance local` 装 per-instance LaunchAgent。恢复矩阵一句话：ngrok 网络闪断自己重连；bridge/ngrok 真退出由 supervisor 退避补起；supervisor 崩溃、logout、reboot 由 launchd 拉回；维护窗口用 pause marker 停住 local（supervisor 存活，launchd 不 crash-loop），清 marker 即恢复，不碰 hpc。launchd 真实装载在 2026-08-14 的 host round-trip 里完成：真实 TERM 一个 managed bridge 进程，supervisor 用新 PID 补起，local 和公网 health 恢复，ngrok 没被误杀。

**已知限制。** thread 状态在 app-server 进程内存里，Bridge 重启即丢，长任务依赖调用方循环 observe；没有多租户、审计和审批流；只在 macOS arm64 和 codex 0.147.0 上实测过。运行时日志包含 thread 内容和命令文本，别往日志里放秘密。

**想自己试的最短路径。** 不走公网的最小验证是本地模式：

```text
openssl rand -hex 32 > .bridge_api_key && chmod 600 .bridge_api_key
export BRIDGE_API_KEY="$(cat .bridge_api_key)"
python3 -m http_server --host 127.0.0.1 --port 8321
```

然后 curl 一下 http://127.0.0.1:8321/health，通说明桥和 app-server 都活着，之后就能按 openapi.yaml 里的请求示例调动作。本地模式不经 ngrok，适合先验证协议行为，再决定要不要接公网和 Custom GPT Actions。

要走公网，先有 ngrok 固定域名，装好 Bridge 之后用 start_ngrok_bridge.sh 一把起，脚本做三段 health 验证，全绿后把 OpenAPI 部署副本里的域名换成真的，在 ChatGPT 里建 Custom GPT，导入这份 OpenAPI，填上同一个 key。到这一步，手机上的对话就能驱动本地 Codex 了。

如果你也在折腾手机讨论、电脑执行这类流程，可以先用本地模式把协议跑通，再决定要不要开公网口。开之前把 key 分发、沙箱模式、目录守卫这三件事想清楚，想不清楚就先别开。

这套东西现在是我日常的一部分，也一直在往里补边界。文章里的数字和口径都来自当前工作区的真实状态，版本时点以 2026-08-14 v1.1.0 发布为准（发布状态待最终核对）。
