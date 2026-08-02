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
session_id
run_id
group_id?
sequence
event_type
actor_type
actor_id
target_type?
target_id?
entity_type
entity_id
entity_version
visibility
summary
payload
created_at
```

典型事件包括：

- Run 创建、等待、结算和取消；
- Work Item 状态与结果变化；
- AgentInvocation 请求与定向结果；
- ToolInvocation 等待资源、等待确认、完成、失败或结果未知；
- Clarification 发起和回复；
- Message 写入；
- Artifact 发布；
- Plan / Group Proposal 确认。

关键业务状态与对应 ExecutionEvent 必须在同一数据库事务提交。事件引用实体版本，并对同一实体版本和事件类型设置幂等约束。

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

Observability 数据允许批量、异步导出和独立保留，可能比业务状态晚出现。它不是可靠通知源；即使后期采样或导出失败，也不能影响 ExecutionEvent、Checkpoint 或 Run。

第一阶段可以 100% 采集，但不得记录平台密钥、凭证、模型私有推理或未经控制的敏感全文。模型 Token 流可以实时展示，但不逐 Token 持久化为 ExecutionEvent。

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
