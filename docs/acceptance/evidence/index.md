# 一期验收证据索引

所有截图来自 `http://localhost:8081` 的隔离测试栈，未包含模型密钥、账号密码、Token 或 Authorization Header。

| 编号 | 文件 | 证据说明 |
| --- | --- | --- |
| E01 | [01-model-connection.png](01-model-connection.png) | `qwen3.5-plus` 连接测试成功 |
| E02 | [02-skill-published.png](02-skill-published.png) | `retail-evidence-analysis` v1 发布及 checksum 前缀 |
| E03 | [03-after-sales-agent-published.png](03-after-sales-agent-published.png) | 售后判责专员装配与发布 |
| E04 | [04-five-agents-published.png](04-five-agents-published.png) | 五个零售垂类 Agent 发布状态与 Group 开关 |
| E05 | [05-chat-after-sales-completed.png](05-chat-after-sales-completed.png) | 售后判责 Chat 完成及 Artifact 发布 |
| E06 | [06-plan-proposal-first-failure.png](06-plan-proposal-first-failure.png) | Plan 首次生成因上游流中断失败 |
| E07 | [07-plan-four-node-completed.png](07-plan-four-node-completed.png) | 四节点 Plan revision 2 冻结并全部成功 |
| E08 | [08-group-initial-completed-clarification-blocked.png](08-group-initial-completed-clarification-blocked.png) | Group 首轮结果及定向澄清等待状态 |
| E09 | [09-group-required-action-notification-only.png](09-group-required-action-notification-only.png) | Required Action 全局通知；回答入口需切回 Chat 视图 |
| E10 | [10-version-freeze-v3-chat.png](10-version-freeze-v3-chat.png) | 总控发布 v3 后新 Chat 返回 `[UAT-V3]` |
| E11 | [11-tool-governance-never-success.png](11-tool-governance-never-success.png) | `execute` 设为 `never` 后无确认成功返回 81 |
| E12 | [12-group-tool-boundary-result.png](12-group-tool-boundary-result.png) | Group 图外营销 Agent 调用边界结果 |

## API / Trace 证据摘要

- Chat Run：`019fc583-f73a-7cc5-9ab2-cc079e1c8ccd`，完成事件 sequence 44。
- Chat Skill checksum：`28f6392d1ca5ebafa3770065516c362b8ee4883e11198f79708b859352377613`。
- Plan：`019fc58a-f6ad-722d-bc80-61136514edb7`，冻结哈希 `c3fda8a7812fbb93fd46f24a560660b2d038d4078e632639d8907f13a9a62f1b`。
- Group：`019fc592-f2e0-79fd-b364-aa3a36ceaeff`，内容哈希 `57c39cb5d20cde1d6d8fe93c7477484444c3988e73303907d4082d7477ae1644`。
- Worker 恢复 Run：`019fc5ab-0b2f-7817-9dca-ba022ba78d55`；epoch 1 Runner `lost`，epoch 2 Runner 成功；仅一个 `run.completed`（sequence 41）。
- 图外调用 Run：`019fc5b3-de4f-7f26-b97a-eb34f3cfd6c4`；存在 `agent_invocation.rejected`（sequence 79），Group Session 保持 `open`。

详细判定、缺陷和规避方案见[一期真实业务 UAT 报告](../phase-one-uat-2026-08-03.md)。
