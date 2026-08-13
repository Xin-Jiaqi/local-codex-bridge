# Unattended Automation（本机无人值守接管）

> 类型：项目级通用设计（framework）· 日期：2026-08-13
>
> 本文档描述「ChatGPT 在线规划/监督 + 离线后本机 monitor 周期接管 + 终态
> 邮件通知」的通用规则与安全边界，不绑定任何具体科研 case。具体项目的私有
> 配置（label、检查频率、邮件通道与 sender/recipient、脚本路径等）属于
> 项目私有配置，不写入本公共仓库文档。

## 职责分工

- ChatGPT 在线时：负责规划、监督与 review；长任务按
  `docs/chatgpt-codex-operating-policy.md` 的 Long-running task completion
  protocol 持续 observe 到终态，或在硬限制下显式标记 `PENDING`。
- ChatGPT 离线 / 回复结束后：由 LaunchAgent 按固定周期运行本机 monitor 接管
  检查。monitor 不是 ChatGPT，不做高层策略改写，不重新设计任务，只按项目
  既定安全规则执行。

## Monitor 行为

- 读取 project checkpoint / state / log，判断任务状态：
  - running：继续周期检查；在项目既定规则内，必要时调用本机 Codex 执行
    窄范围、边界明确的任务；
  - terminal：进入终态处理（见下）；
  - blocked / 未知高风险：写 `BLOCKED`，不做危险操作，等待 ChatGPT / 用户。
- 调用 Codex 前按项目既定安全规则校验（工作区、权限、允许的命令与工具范围）。
- 动态识别 active / recent job（例如按 checkpoint / state 中最近更新的
  job），不硬编码 job id。

## 终态处理

- 终态（COMPLETE / FAILED / CANCELLED / TIMEOUT 等）第一次出现时：
  1. 收集 case / job / state / exit / time / log 摘要；
  2. 发送邮件通知；
  3. 写入 terminal marker。
- 无论邮件发送成功还是失败，都写入 terminal marker，并停止自动 Codex 调用，
  避免 API 消耗；通知失败不得造成无限模型调用。
- 同一终态只通知一次（由 terminal marker 保证）。

## 安全边界

- 邮件凭据只放 macOS Keychain，不写入脚本 / git / log / checkpoint。
- monitor 只做窄范围、可回滚的本地操作；未知或高风险情况写 `BLOCKED`，
  等待 ChatGPT / 用户，不做危险操作。
- 终态后 monitor 停止调用 Codex；rearm 之前不再自动发起模型调用。
- 新任务通过 rearm 清 terminal marker；rearm 不提交作业、不改业务状态。

## 项目私有配置（不写入本仓库）

本文件是 framework。具体项目的任务 label、检查频率、邮件通道（sender /
recipient / 具体服务）、monitor 与脚本路径、作业提交细节等均为项目私有
配置，仅存在于项目私有位置，不写入本公共仓库文档。
