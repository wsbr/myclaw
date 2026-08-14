# 执行事件、Checkpoint 与可观测性

本文是可靠执行时间线、恢复快照和 Observability Trace 的唯一权威来源。ExecutionQueue、Outbox 和 Worker 并发见 [Runtime 调度与数据库并发](runtime-scheduling.md)。

## 四层分工

MyClaw 不采用完整 Event Sourcing，也不使用 Trace 推导当前业务状态：

```text
领域状态表
├── ExecutionEvent：可靠执行账本
├── RunnerCheckpoint：崩溃恢复
└── Outbox：可靠后台命令，详见调度模块

Observability Trace / Span
└── 调试、性能、Token 与调用链
```

Session、Run、WorkItem、ToolInvocation、AgentInvocation、Clarification 和 Artifact 等领域表保存当前权威状态。

## ExecutionEvent

ExecutionEvent 记录需要被用户、API、Group 或审计可靠观察的业务变化：

```text
event_id
tenant_id
session_id
run_id?
sequence
event_type
visibility
actor_kind
actor_account_id? / actor_agent_id? / actor_runtime_id?
aggregate_type / aggregate_id / aggregate_version
cause_type / cause_id
dedupe_key
safe_summary
payload_json
previous_event_hash / event_hash
hash_schema_version
occurred_at
```

典型事件包括：

- Run 创建、等待、结算和取消；
- Work Item 状态与结果变化；
- AgentInvocation 请求与定向结果；
- ToolInvocation 等待资源、等待确认、完成、失败或结果未知；
- Clarification 发起和回复；
- Message 写入；
- Artifact 发布；
- Session mode、Plan Approval 和 permission mode 变化；
- WorkingPlan、Group Membership、Group Message、SharedTask 和 Verification 变化。

关键业务状态与对应 ExecutionEvent 必须在同一数据库事务提交。事件引用实体版本，并对同一实体版本和事件类型设置幂等约束。

Session sequence 分配的同一串行点还要按[数据库契约](../technical/database-schema.md)的 `hash_schema_version` 计算 canonical event hash chain。hash 只是篡改检测，不替代数据库权限、备份或外部审计；`enterprise_production_v1` 至少每日验链，由 KMS 签名 head 并把 seal 写入不可覆盖存储。用户/Group SSE 经过 visibility 过滤，不对外声称客户端能验证一条包含不可见事件的完整链。

### 稳定 event_type

event_type 使用 `aggregate.action` 小写 snake_case，不创建同义别名。v1 稳定集合：

```text
run.created | run.waiting | run.resumed | run.cancellation_requested
run.completed | run.failed | run.cancelled
work_item.created | work_item.running | work_item.waiting_user
work_item.waiting_approval | work_item.waiting_tool | work_item.waiting_agent
work_item.waiting_resource | work_item.resumed | work_item.succeeded
work_item.failed | work_item.cancelled | work_item.timed_out | work_item.blocked
runner.created | runner.started | runner.succeeded | runner.failed | runner.cancelled | runner.lost
agent_turn.started | agent_turn.waiting_actions | agent_turn.completed | agent_turn.failed
message.created
tool_invocation.created | tool_invocation.waiting_confirmation
tool_invocation.waiting_resource | tool_invocation.queued | tool_invocation.running
tool_invocation.succeeded | tool_invocation.failed | tool_invocation.rejected
tool_invocation.cancelled | tool_invocation.outcome_unknown
agent_invocation.created | agent_invocation.queued | agent_invocation.running
agent_invocation.succeeded | agent_invocation.failed | agent_invocation.rejected
agent_invocation.cancelled | agent_invocation.timed_out
clarification.requested | clarification.answered | clarification.cancelled
approval.requested | approval.approved | approval.rejected | approval.cancelled
plan.permission_entered | plan.permission_exited | plan.implementation_approved
working_plan.updated
group.created | group.membership_joined | group.membership_ended | group.membership_invitation_rejected
group.message_created | group.summary_requested | group.summary_delivered
group.diagnostic_recorded
shared_task.updated
artifact.published | artifact.unavailable | artifact.restored | artifact.deleted
notification_delivery.created | notification_delivery.succeeded
notification_delivery.failed | notification_delivery.dead | notification_delivery.outcome_unknown
verification.completed | budget.warning | late_result.observed
```

对象没有发生状态迁移时可以不写事件；如果需要增加事件，必须同时更新数据库/API/OpenAPI 可见 payload 测试。`message.delta` 与 `stream.degraded` 是 Redis provisional frame，不是 ExecutionEvent，不能出现在可靠 event_type 列表或占用 Session sequence。

可靠事件 payload v1 只允许 `schema_version, resource_href, from_status, to_status, reason_code, message_id, action_id, artifact_id, related_ids, changed_fields`；正文和详细对象由授权客户端通过 resource_href 获取。新增 payload 字段必须更新 OpenAPI 并做 visibility/脱敏测试，不能把任意领域 JSON 原样塞入 Event。

ExecutionEvent 追加后不修改。后续状态变化追加新事件。当前状态读取领域表，历史过程读取 ExecutionEvent；历史中的旧状态不是不一致。

SSE 只发送已经提交的 ExecutionEvent。客户端使用 sequence 断线续读，并使用 event_id 去重。网络交付采用至少一次语义，不承诺恰好一次。

Group 共享查询只能读取对该 Group 公开的 Message 和 ExecutionEvent。Agent 私有 Prompt、Instructions、Memory、未公开 Tool 数据和模型内部推理不能通过事件泄露。

## RunnerCheckpoint

Checkpoint 只保存恢复执行所需的数据：

```text
runner_id
work_item_id
checkpoint_type
state_snapshot
last_completed_step
created_at
```

Runner 进程崩溃后，平台在同一 Work Item 下创建新 Runner，并从最新安全 Checkpoint 继续。已经成功的 ToolInvocation 复用持久化结果；不会因为 Runner 重建而重新产生业务调用。

Checkpoint 不是用户时间线，不承诺精确重现模型行为。其保留周期可以独立治理。

## Observability Trace / Span

Trace 遵循 OpenTelemetry 和 GenAI 语义约定，用于开发调试和生产观测，不参与业务状态判断。

MyClaw 的 Plan 和 Group 可能跨越很长时间，因此每个 Runner 创建独立 Trace：

```text
Runner Trace
├── context_assembly Span
├── history_selection Span
├── memory_recall Span
├── model_generation Span
├── tool_execution Span
├── invoke_agent Span
└── result_assembly Span
```

Trace 使用 `session_id`、`run_id`、`work_item_id`、`runner_id`、`agent_id` 等属性关联。异步 Tool 和 AgentInvocation 跨 Runner 时使用 Invocation ID 与 Span Link 连接，而不是让一个 Span 跨数小时保持打开。

Span 可以记录：

- Agent、Model、Tool 和策略标识；
- 开始、结束、延迟与错误；
- Token 与资源用量；
- ToolAttempt 次数；
- 经过脱敏的输入输出摘要；
- Artifact 和领域实体引用。

Runner 诊断还必须区分 `assembled_tool_keys` 与实际 `effective_tool_keys`，并记录 runtime release、Harness/Context/Compaction/Continuation Policy、Provider Adapter、Tool Schema bundle、experiment cohort、permission mode、WorkingPlan revision、GroupRunSnapshot、Context manifest、AgentTurn、ProviderAttempt、stop reason、累计预算、ActionDecision、Invocation、Verification 和 terminal reason。诊断字段用于解释执行，不能反向决定业务状态。

平台不得采集或推断模型私有 Chain-of-Thought。需要解释决策时保存模型主动给出的 bounded decision summary、Evidence 引用和风险；Trace 不保存隐藏推理、完整 Prompt、凭证或未经脱敏的业务全文。

Observability 数据允许批量、异步导出和独立保留，可能比业务状态晚出现。它不是可靠通知源；即使后期采样或导出失败，也不能影响 ExecutionEvent、Checkpoint 或 Run。

UAT/生产不能只启用 debug exporter。至少一个出口必须有可验证的持久化或可查询后端、明确保留期和真实写入烟测；部署健康检查不能用“Collector 进程已启动”替代“Trace 已成功写出”。

第一阶段可以 100% 采集，但不得记录平台密钥、凭证、模型私有推理或未经控制的敏感全文。模型 Token 流可以实时展示，但不逐 Token 持久化为 ExecutionEvent。

## Runtime 管理控制台

管理员可通过 Web 的 `Runtime Console` 或 `/admin/runtime/*` 只读投影回答当前 Run 状态、等待原因、下一执行实体、预算用量、最近进展、Runner/Tool 租约、Provider/Tool/Agent 调用、Group 诊断、Outbox 积压与终态原因。Run 详情时间线直接来自权威 ExecutionEvent，控制台不保存第二套 lifecycle，也没有任意修改数据库状态的入口。

跨账号运营投影只返回 ID、状态、计数、稳定错误码、受控摘要与时间；不返回 Message/WorkItem 正文、AgentInvocation request_text、Tool 参数/结果、Outbox payload、raw error detail、Prompt 或 Chain-of-Thought。当前 Worker 列表明确标记为“活跃租约观测”，只能证明正在持有 Runner、ToolAttempt 或 Outbox lease 的 Worker，不能冒充完整的空闲 Worker 注册表；持久化 Worker capacity/idle heartbeat 属于后续独立注册能力。

## Usage / 成本账本

管理端 `Usage / 成本` 页面与 `/admin/usage/*` API 直接投影 append-only 账本。ProviderAttempt 自动记录请求数、输入/缓存输入/输出 Token 和活跃毫秒；ToolAttempt 自动记录 Tool unit 与 Sandbox 毫秒。失败、重试与 `outcome_unknown` 和成功 Attempt 使用同一口径，Group 汇总因此自然包含全部成员和返工，而不是只统计最终 Reporter。

价格不是模型配置上的可变数字，而是按 subject/provider/metric 与生效窗口选择的不可变 `usage_price_snapshot`。每个已知成本条目复制所用 currency、unit price 与 price status；找不到价格或计量不可信时 `cost_status=unknown` 且 amount 为 NULL。运营归集必须分别显示 `estimated`、`contracted` 与 `unknown`，修正通过受审计的 adjustment 追加，不能回写历史。

## 恢复、重新执行与 Replay

第一阶段支持：

- 从最新安全 Checkpoint 恢复技术故障；
- 在非终态 Run 中重试同一 Work Item；
- 从终态结果创建引用历史的新 Run。

终态 Run 不重新激活。新执行可以记录：

```text
source_run_id
source_work_item_id
```

但使用新 Runner 启动时的有效 Agent、Model、Tool、Skill、Policy 和外部业务数据。

第一阶段不支持从任意历史节点 Fork，也不承诺确定性 Replay。模型非确定性、外部数据变化和不可逆 Tool 副作用使精确重放不能由 Trace 保证。只读模拟 Replay 和历史环境保留属于项目后期能力。
