# 第一阶段状态机

本文是第一阶段领域状态迁移的唯一实现级定义。所有迁移均为比较并交换：`WHERE id=? AND version=? AND status IN (...)`；成功后递增 version，并与所属事件账本、必要 Outbox 同事务提交。Session 执行对象写 ExecutionEvent；无 Session 的管理对象写 PlatformAuditEvent，边界见 [事件与可见性](events-audit-visibility.md)。

## 通用规则

- “发起者”是获准提交状态命令的角色/服务，不代表它可绕过领域前置条件。
- 终态不可复活。重试或后续工作创建新 Runner、ToolAttempt、Run、PlanExecution 或 GroupRun。
- 取消采用 `cancellation_requested_at` 意图 + 真实状态结算；已完成事实不改写为取消。
- 条件更新影响 0 行时重新读取：若目标状态已达到则幂等成功；若进入其他合法状态则返回当前事实；否则 `409 version_conflict`。
- 迟到结果始终产生带独立 cause dedupe key 的 `*.late_result_observed` ExecutionEvent；只允许补充诊断/Attempt 结果，不恢复终态聚合、不触发新 Runner。

## 1. Agent 与 AgentVersion

Agent 状态：`draft → published ↔ inactive`。AgentVersion 状态不是发布原地迁移：可编辑 `draft` 被复制为新的不可变 `published`；`published → deprecated`；`draft_test_snapshot` 创建即不可变并在保留期后清理。

| 迁移 | 发起者与前置条件 | 终态 | PlatformAuditEvent | Outbox/并发语义 |
| --- | --- | --- | --- | --- |
| 新建 Agent + draft | builder/admin；有创建权限 | 否 | `agent.created`、`agent_version.draft_created` | 无；同事务 |
| draft 保存 | owner builder/admin；目标是当前 draft | 否 | `agent_version.draft_updated` | 无；`If-Match` 防覆盖 |
| publish copy | owner builder/admin；发布校验全部通过 | 新 published Version 对执行不可变 | `agent_version.published`、首次 `agent.published` | 锁 Agent/draft，复制标量与关系，分配 version_no，更新 current pointer；原 draft 保留并 rebase |
| 创建 draft_test_snapshot | owner builder/admin；draft revision 匹配 | Snapshot 不可变 | `agent_version.draft_test_snapshot_created` | 与 draft_test Run 创建同事务；不允许被正式调用 |
| published → inactive | owner/admin | 否 | `agent.deactivated` | 不取消已确认执行；阻止新 Run/Proposal |
| inactive → published | owner/admin；仍有当前 published version | 否 | `agent.reactivated` | 不复活旧执行 |
| Version published → deprecated | owner/admin；无新执行选择 | 是（仍保留） | `agent_version.deprecated` | 已固定执行继续 |

发布事务内从不把 draft 行改为 published，也不创建第二个可编辑 draft。发布失败不改 Agent 指针、next_version_no、draft_revision 或关系行，并写脱敏的 `agent.publish_failed` PlatformAuditEvent（验证失败可以在回滚后的独立审计事务记录）。inactive Agent 发布新版本仍 inactive。不得“激活草稿”；Agent 至少有一个 published version 才可处于 `published`。

### 管理 Registry 事件归属

Model、PlatformTool、Skill/SkillVersion、PolicyDefinition、SandboxProfile、账号、角色、API Key 与 ChannelAccount 的创建、编辑、启停、发布、轮换、撤销和 reconcile 都写 `platform_audit_event`，不写 ExecutionEvent。若操作需要后台动作，则状态 + audit + Outbox 同事务，例如 Skill 发布写 `skill.version_published` audit 与 `runtime.maintenance:sync_skill`；Tool 停用只写 `platform_tool.disabled` audit，随后受影响的具体 ToolInvocation 各自在所属 Session 写 `tool.failed` ExecutionEvent。普通校验失败不产生 Outbox。

## 2. UserSession

状态：`active → closed → archived`；`active → archived` 仅用户显式删除时由服务内部先等价关闭。

| 迁移 | 发起者/前置 | 终态 | Event | Outbox |
| --- | --- | --- | --- | --- |
| create active | user/API/Channel；入口 Agent 可用，Sandbox 可创建 | 否 | `session.created`、`sandbox.provisioned` | Sandbox 需异步时 `runtime.sandbox` |
| active → closed | 所有者；不再接受新输入 | 否 | `session.closed` | 取消非终态 Run 的 control 命令 |
| closed → archived | 所有者/Retention | 是（只读） | `session.archived` | Workspace cleanup（显式删除/策略命中） |

Session 不因空闲、页面关闭或 Run 完成迁移。Sandbox provisioning 失败则创建事务回滚，或 Session 保持 active 且 Sandbox failed 时拒绝 Run；第一阶段推荐同步创建目录后再提交 Session。

## 3. Run

状态：`running ↔ waiting → settling → completed`，任一非终态可因取消进入 `cancelled`；`outcome` 只在 completed 时必填。

| 迁移 | 发起者/前置 | Event | Outbox |
| --- | --- | --- | --- |
| create running | API/control；已创建首 WorkItem | `run.created` | `runtime.runner` 随首 Runner |
| running → waiting | Runtime；无 active Runner，存在非终态等待 WorkItem | `run.waiting` | 无，等待对象负责恢复 |
| waiting → running | control；至少一等待满足且可建 Runner | `run.resumed` | `runtime.runner` |
| running/waiting → settling | Runtime/control；所有相关工作已终态或明确收敛 | `run.settling` | `runtime.control:settle_run` |
| settling → completed | 交付 Agent/settler；结果 Message 已提交 | `run.completed` | `runtime.maintenance:extract_memory`；仅Channel Session创建Delivery并发`runtime.delivery` |
| 非终态 → cancelled | 用户/API；取消传播已落库，未完成项已收敛 | `run.cancelled` | control/tool cancel；仅Channel Session创建Delivery |

`completed`、`cancelled` 为终态。取消先到时设置 cancellation intent，运行中结果若先成功提交则保留成功并进入 settling；取消方随后只能停止后续工作。`completed` 的 outcome 为 `succeeded/partially_succeeded/failed`，`cancelled` 不填 outcome。

## 4. WorkItem

状态：`queued → running → waiting_* → queued`；从 queued/running/waiting 可到结果终态 `succeeded/failed/timed_out/cancelled/abandoned/blocked`。

| 迁移 | 发起者/前置 | Event | Outbox |
| --- | --- | --- | --- |
| create queued | Runtime/Plan/Group scheduler；依赖满足或目标调用已授权 | `work_item.created` | 创建 Runner 时发 runner 命令 |
| queued → running | Runner 取得 lease | `work_item.started` | 无 |
| running → waiting_user | Runner 创建 open Clarification/Confirmation + safe Checkpoint，清 active_runner | `work_item.waiting_user` | 仅Channel Session创建NotificationDelivery/命令 |
| running → waiting_agent | 已创建一批 AgentInvocation 与目标 WorkItem，清 active_runner | `work_item.waiting_agent` | 目标 runner 命令 |
| running → waiting_tool | Durable ToolInvocation 已受理，清 active_runner | `work_item.waiting_tool` | tool/sandbox 命令 |
| running → waiting_resource | Workspace lock request queued，清 active_runner | `work_item.waiting_resource` | lock 获得时 control 恢复 |
| waiting_* → queued | 对应条件满足、Run 未取消、无 active Runner | `work_item.resumed` | 新 Runner |
| running → succeeded/failed | Agent 语义结果或明确错误，清 active_runner | `work_item.succeeded/failed` | 上游/Run settle control |
| 非终态 → timed_out | control；平台/业务明确期限命中 | `work_item.timed_out` | 上游/settle |
| 非终态 → cancelled/abandoned/blocked | cancel/settler；分别表示用户取消、主动收敛、依赖终态阻断 | 对应事件 | 取消下游、上游 settle |

所有结果状态终态。Clarification 默认无业务超时。技术 Runner 超时不直接令 WorkItem timed_out；可安全恢复时建新 Runner。等待 all-settled AgentInvocation 时只有整批终态才恢复。

## 5. Runner

状态：`queued → running → succeeded/failed/cancelled/lost`。

- `queued → running`：runtime worker 已 claim `runtime.runner` Outbox 后，在独立短事务校验 active_runner/execution_epoch并写 lease_owner/token/expiry；事件 `runner.started`。事务提交后立即 ack Outbox，之后才执行模型；Outbox claim 不覆盖长循环。
- `running → succeeded/failed`：持有当前 lease_token 且 WorkItem execution_epoch 匹配；事件 `runner.succeeded/failed`；根据结果在同事务改变 WorkItem，并可能写 control/runner/tool Outbox。
- `queued/running → cancelled`：Run/WorkItem 已取消且执行尚可停止；事件 `runner.cancelled`。
- `running → lost`：control 检测租约过期；事件 `runner.lost`；安全 Checkpoint 存在且 WorkItem 非终态时创建全新 queued Runner + `runtime.runner`，否则 WorkItem failed。

四个结果均终态。旧 lease_token 的任何 Message、Checkpoint、Invocation 或结算写入返回 fencing conflict；其外部调用若迟到，只能关联已有 Invocation 处理。

## 6. ToolInvocation 与 ToolAttempt

ToolInvocation：`created → waiting_confirmation/waiting_resource/queued → running → succeeded/failed/rejected/cancelled/outcome_unknown`，可从 `running → cancel_requested → cancelled/succeeded/failed/outcome_unknown`。

| 迁移 | 发起者/前置 | Event | Outbox |
| --- | --- | --- | --- |
| create | Runner；Tool 在快照、参数 Schema 合法，持久化幂等键 | `tool.created` | 取决于后续状态 |
| created/queued → waiting_confirmation | Dispatcher；当前 policy=always 且未批准 | `tool.confirmation_required` | delivery；Runner 结束 |
| waiting_confirmation → queued | 原 user 批准且 hash/principal 未变 | `tool.approved` | inline 由新 Runner，async 发 tool/sandbox |
| waiting_confirmation → rejected | 原 user 拒绝 | `tool.rejected` | control 恢复 Agent 看最终结果 |
| created/queued → waiting_resource | 需要锁且未获锁 | `tool.waiting_resource` | 无 |
| waiting_resource → queued | 锁授予且 Tool 仍启用/授权有效 | `tool.resource_acquired` | tool/sandbox 或恢复 Runner |
| queued → running | Inline 当前 Runner 或 Tool Worker；执行前再查 enabled/policy | `tool.started` | 无 |
| running → succeeded/failed | Attempt 确认结果 | `tool.succeeded/failed` | control 恢复 WorkItem |
| running → outcome_unknown | 有副作用且无法确认远端结果 | `tool.outcome_unknown` | control 恢复，禁止自动重试 |
| 非终态 → cancelled | 尚未产生不可取消完成事实 | `tool.cancelled` | control 恢复 |

ToolAttempt：`queued → running → succeeded/failed/timed_out/cancelled/outcome_unknown`；失败且 `retryable=true`、预算未耗尽、Invocation 未取消时创建新 Attempt（不是复活）并写相同 Topic Outbox。Invocation 只暴露最终状态。停用 Tool 会把尚未 started 的调用结算 `failed(tool_disabled)`；已 running 不强停。`never→always` 在 started 前转等待；`always→never` 不自动批准既有等待。

## 7. AgentInvocation

状态：`created → queued → running → succeeded/failed/cancelled/timed_out/rejected`。

- 创建者是持有 lease 的调用方 Runner；必须通过模式范围、绑定/图、用户访问与 `invoke_agent` 装配校验，否则直接 `rejected` 并记录 `agent_invocation.rejected`。
- `created → queued` 与目标 WorkItem/Runner 创建同事务，事件 `agent_invocation.queued`，Outbox `runtime.runner`。
- 目标首 Runner 启动时 `queued → running`；目标 WorkItem 终态映射为 Invocation 终态并写 `agent_invocation.*`，control 检查 batch all-settled 后恢复调用方。
- 取消只阻止后续推进并传播目标 WorkItem；目标先完成则保留真实结果。终态结果迟到不会唤醒已终态调用方 Run。

所有结果状态终态；业务再次请求必须创建新 AgentInvocation。

## 8. Clarification 与 Tool Confirmation

Clarification：`open → answered/cancelled`。只有目标 userId 可回答；`open → answered` 必须创建 answer Message、更新 Clarification、写 `clarification.answered` 和 `runtime.control:resume_work_item` 同事务。重复相同 idempotency key 返回原 answer，不同 answer 返回 409。Run 取消时 open → cancelled。Answered 后迟到答案返回 `409 action_already_resolved`，不新增 Message。

Tool Confirmation：`pending → approved/rejected/cancelled`。只有 Invocation 原 userId 可决定；批准要求 tool_key、arguments_hash、principal_hash 与待确认记录一致。批准/拒绝事件为 `tool_confirmation.approved/rejected`，并写 control/tool Outbox。三者终态。平台策略放宽不迁移 pending；Tool 停用或 Run 取消可将其 cancelled。

## 9. PlanExecution

Plan 只有定义状态 `draft → awaiting_confirmation → frozen`，确认前可 `→ cancelled`。执行 lifecycle/outcome 不在 PlanExecution 重复存储，直接使用一对一 Run；公开 Plan 状态是 definition_status 与 Run 的组合。

| 迁移 | 发起者/前置 | Event | Outbox |
| --- | --- | --- | --- |
| create draft | Plan Owner Runtime；Proposal 已解析 | `plan.proposed` | 无 |
| draft 编辑 | 用户；未确认，DAG/Agent 合法 | `plan.edited` | 无；graph_revision/version CAS |
| draft → awaiting_confirmation | Owner/API | `plan.confirmation_required` | delivery |
| awaiting_confirmation → frozen | 用户；锁 Run+Plan，重新校验访问/启停，固定全体 AgentVersion 和 frozen_hash | `plan.confirmed`；Run 同事务 waiting→running | 可运行节点 Runner |
| draft/awaiting → cancelled | 用户 | `plan.cancelled`；Run 同事务→cancelled | 取消 proposal work |

`frozen/cancelled` 对定义为终态。确认后所有图定义行拒绝 UPDATE/DELETE。运行、等待、结算、完成与运行中取消只迁移关联 Run；Plan API 映射同一 Run lifecycle/outcome，不再写 `plan.waiting/completed` 镜像状态。计划失效创建 predecessor 引用的新 PlanExecution。固定 Agent 后的停用不终止当前 Plan；Tool/Model 当前停用仍使具体执行明确失败。

## 10. GroupSession 与 GroupRun

GroupFormationProposal：`generating → awaiting_confirmation → confirmed`，以及 `generating/awaiting_confirmation → rejected/cancelled/failed`。

| 迁移 | 发起者/前置 | ExecutionEvent | Outbox |
| --- | --- | --- | --- |
| create generating | API；父 Session active，入口 Agent group_enabled；同事务创建 GroupSession(forming) | `group_formation.started` | proposal generation Runner |
| generating → awaiting_confirmation | generation Runner；关系化 revision 校验通过；generation Run 同事务 completed+succeeded | `group_formation.proposed`、`run.completed` | 仅 Channel Session delivery |
| awaiting 内新 revision | Session 所有者；expected revision/version 匹配，图合法 | `group_formation.revised` | 无 |
| awaiting → confirmed | Session 所有者；锁 current revision，重校验参与者/访问/Sandbox，GroupSession forming→open | `group_formation.confirmed`、`group.opened`、`run.created` | 初始 WorkItem Runner；Channel 状态通知按需 |
| awaiting → rejected | 用户明确拒绝建议 | `group_formation.rejected`、`group.closed` | 无；generation Run 已终态 |
| generating/awaiting → cancelled | 用户/Session 取消；generating 时传播 generation Run | `group_formation.cancelled`、`group.closed` | 仅 generating 时 control cancel |
| generating → failed | 模型/校验形成明确失败 | `group_formation.failed` | 无；Run 结算 failed |

Revision 本身不可变；编辑通过复制关系行生成 `revision_no+1`，CAS 失败返回 409，不覆盖他人编辑。`confirmed/rejected/cancelled/failed` 终态。确认事务从 current revision 打开既有 GroupSession、冻结 Bootstrap/source、节点、建议边、首 GroupRun/Run、初始 WorkItem 与版本映射；建议边不创建实际 collaboration edge。拒绝/取消/失败把 forming GroupSession 转 closed，不留可执行空群。

GroupSession：`forming → open → closed → archived`，forming 也可因 Proposal 拒绝/取消/失败 `→ closed`。open→closed 由用户发起，写 `group.closed` 并给所有非终态 GroupRun/Run 写 cancel control；closed 只读且不重开。closed→archived 由 Retention/用户发起并写 `group.archived`。

GroupRun 不存独立 status/outcome；生命周期完全使用一对一 Run 的 `running/waiting/settling/completed/cancelled`。产品中的 `created` 只是在首个 Runner started 前由 Run/Runner 事实派生的展示 phase，不是持久状态。创建 GroupRun 与 Run 必须同事务，事件只写一次 `run.*`，必要的 Group 语义事件使用独立 dedupe key。Completed/cancelled 由 Run 决定且不可复活，迟到结果只记 cause-specific event。

新 GroupRun 创建事务重新检查全部固定节点并写完整 `group_run_agent_version` 映射：可用节点固定最新已发布版本，不可用节点固定为 unavailable 且版本为空。非 owner 节点不可用不阻塞其他节点；它仍留在图中，但任何给它的新 WorkItem、定向 Message 或 AgentInvocation 都以 `group_node_unavailable` 拒绝，且不得替换或静默删除。

owner 必须可用。用户未指定时选择入口节点；入口不可用则命令以 `group_owner_unavailable` 失败且不创建 Run/GroupRun，用户可显式选择另一个可用固定节点。失败命令写 user 可见 `group_run.creation_rejected` ExecutionEvent，使用命令级 dedupe key，不产生 Runner Outbox；成功事务写 `run.created` 以及每个 unavailable 节点的 user/group 安全摘要 `group_run.node_unavailable`。Agent 后续重新启用只影响未来新建 GroupRun；已创建映射和运行中/终态 Run 均不变化。并发停用与创建按相关 Agent 行锁的提交顺序决定本轮快照，事务内不得出现部分映射。

## 11. NotificationDelivery

状态：`queued → delivering → succeeded`；`delivering → retry_wait → delivering`；非终态可到 `failed/cancelled/outcome_unknown`。

| 迁移 | 发起者/前置 | ExecutionEvent | Outbox |
| --- | --- | --- | --- |
| create queued | Settlement/Required Action 事务；Session 来源为 active Channel binding | `notification.created`（user） | 同事务 `runtime.delivery` |
| queued/retry_wait → delivering | delivery worker；短事务取得 Delivery/Attempt lease并创建 provider idempotency key，随后 ack Outbox | `notification.attempt_started`（private） | 无；Outbox 不等待 provider |
| delivering → succeeded | provider 明确接受并返回 message key | `notification.succeeded`（user） | 无 |
| delivering → retry_wait | 瞬时失败且预算未耗尽 | `notification.retry_scheduled`（private） | 新 cause dedupe key 的延迟 delivery 命令 |
| 非终态 → failed | 永久错误或重试耗尽 | `notification.failed`（user safe summary） | 无；告警可写平台维护命令 |
| queued/retry_wait → cancelled | 对应 action 已解决、Session/Channel 解绑且尚未发送 | `notification.cancelled`（user） | 无 |
| delivering → outcome_unknown | 请求可能送达且 Provider 无查询能力 | `notification.outcome_unknown`（user safe summary） | 禁止自动重投 |

`succeeded/failed/cancelled/outcome_unknown` 为终态。迟到 provider 结果创建新的 Attempt 诊断与 `notification.late_result_observed`，不把终态 Delivery 复活；若可确定原 outcome_unknown 实际成功，只记录诊断和 provider_message_key，不触发第二次投递。Delivery 失败、取消或未知不修改 Run lifecycle/outcome，因为 Run 结果在投递前已经提交。Web/API Session 不创建 NotificationDelivery，分别使用事件 UI 和轮询/SSE。

## 12. Artifact

状态：`published → unavailable/deleted`。

- 发布：Agent/Tool；文件已稳定写入 Workspace，checksum/size 校验完成；事件 `artifact.published`，可写 delivery。
- published → unavailable：Workspace 清理或文件校验失败；维护任务；事件 `artifact.unavailable`。
- published/unavailable → deleted：用户显式删除逻辑引用；事件 `artifact.deleted`。

三者对内容版本均为终态；修改文件发布新 Artifact 并引用 parent。下载与清理竞态以 Workspace 读锁和状态二次校验处理。

## 13. OutboxCommand

状态：`pending → claimed → completed`；`claimed → retry_wait → claimed`；`pending/retry_wait/claimed → dead`；claimed 租约过期回 `retry_wait`。

| 迁移 | 发起者/前置 | Event | 后续命令 |
| --- | --- | --- | --- |
| insert pending | 领域事务 | 通常由同事务领域事件解释 | 无 |
| pending/retry_wait → claimed | Topic Worker；available、行锁领取 | 不写用户事件 | 无 |
| claimed → completed | 同 claim_token；已可靠取得独立 Runner/Attempt/Delivery lease，或领域状态证明重复/不适用 | 不写事件 | 无；不等待模型/Tool/脚本/投递完成 |
| claimed → retry_wait | 瞬时基础设施错误 | 可写运维 Trace | 自身 available_at 重投 |
| claimed/retry_wait → dead | 不可重试或超预算 | Session 关联命令写 `outbox.dead` ExecutionEvent；平台命令写 PlatformAuditEvent/告警 | 人工处理，不自动复制命令 |

`completed/dead` 终态。Outbox 完成只表示命令被领域 handler 接受或幂等忽略，不表示长期业务工作完成。
