# Runtime 执行

本文是 Run、Work Item、Runner、执行身份、Prompt、可恢复等待、超时、取消和结算的唯一权威来源。ExecutionEvent、Checkpoint 和 OTel Trace 见 [执行事件与可观测性](observability.md)；ExecutionQueue、Outbox、Worker 租约和数据库并发见 [Runtime 调度](runtime-scheduling.md)。

## 执行对象

### Run

Run 是一次可结算的用户执行周期。Chat 请求、PlanExecution 和 GroupRun 都使用相同的生命周期与结果维度：

```text
lifecycle: running | waiting | settling | completed | cancelled
outcome: succeeded | partially_succeeded | failed
```

Run 可以是 `completed + failed`：执行已经形成完整汇报，但业务目标没有达成。终态 Run 不重新激活。

### Work Item

Work Item 是可以跨分钟、天和用户离线周期持续的业务工作。它保存目标、执行归属、状态、结果和关联 Runner。

成功、失败、超时、取消、放弃和依赖阻塞都是明确终态结果。每个结果使用统一信封：

```text
status
summary
result_data
artifact_ids[]
error
retryable
started_at
finished_at
```

失败是结果，不等于执行仍未完成。

### Runner

Runner 是 Agent 的一次短生命周期执行尝试。它不可原地恢复，也不作为长期挂起进程。

当 Agent 等待用户、其他 Agent 或持久化异步 Tool 时：

```text
Runner 1：产生等待对象后结束
Work Item：waiting_*
等待条件满足
Runner 2：在同一个 Work Item 下继续
```

用户关闭页面或离线不等于暂停或取消。只有 Agent 确实需要输入时，Work Item 才进入 `waiting_user`。

### Invocation

- `ToolInvocation`：一次语义上的 Tool 调用，可包含多个平台重试 ToolAttempt；
- `AgentInvocation`：经 `invoke_agent` 发出的定向 Agent 请求；
- `Clarification`：Agent 等待用户回答的持久化对象。

调度队列是基础设施实现，不把 Celery Task、OutboxCommand 或 MQ Message 暴露为产品领域模型。

## ExecutionPrincipal

每次执行携带不可由 Agent 修改的身份上下文：

```text
tenant_id
userId
authentication_source
api_key_id?
user_session_id
entry_agent_id
current_agent_id
plan_id / group_id
group_run_id
delegation_chain[]
```

Agent-to-Agent 调用代表原始 `userId`，不能通过调用另一个 Agent 提权。目标 Agent 带来自己的领域装配，但有效访问仍受以下交集约束：

```text
原始主体权限
∩ 当前 Agent 显式装配能力
∩ 当前 Plan / Group 范围
∩ Tool 与业务系统访问策略
```

平台服务凭证只提供技术连接能力，不等于原始用户拥有全量业务数据权限。Web、API、Channel 和 AgentInvocation 必须传播同一 `userId`，完整身份契约见 [身份与 Agent API](identity-api.md)。

## Runner 启动

Runtime 在启动时：

1. 加载 Chat Run 解析的最新已发布 AgentVersion，或 Plan / Group 确认时固定的 AgentVersion；
2. 解析唯一 Model；
3. 校验 Session 固定的 Sandbox Profile 并挂载 Workspace；
4. 解析显式 Tool 集合；
5. 解析每个 Skill 的当前发布版本与共享路径；
6. 解析四类 Policy；
7. 解析当前模式允许的 Agent Catalog；
8. 加载 Work Item、当前输入和关联 UserSession / Plan / Group 上下文；
9. 固定 ExecutionPrincipal 与实际配置快照后执行。

解析失败应明确终止，不通过模型 fallback、删除无效项或隐式 Tool 补齐继续运行。

## Prompt 组装顺序

```text
Platform Runtime Rules
Agent name/description/instructions
已装配 Tool Definitions
Skill Catalog
显式绑定 Agent Catalog
Plan Declared Agent Catalog（Plan Owner）
Group Agent Catalog（Group）
Context Policy 结果
History Policy 结果
Memory Recall 结果
当前 Work Item 与执行上下文
当前定向输入
```

Skill Catalog 只含名称、描述和共享路径。Agent Catalog 只含 `id + name + description`。每段保留来源和版本信息。

## 模型与 Tool 循环

Runtime 只调用 Runner 快照中的模型和 Tool：

1. 提交 Prompt 与 Tool Definitions；
2. 模型返回文本、Tool 调用或受控 Runtime 动作；
3. Tool Dispatcher 校验 Tool、输入、身份与模式范围；
4. Tool 需要确认时创建 required action 并结束 Runner；
5. Inline Tool 返回后当前 Runner 可以继续；
6. Durable Async Tool 受理后结束 Runner，结果到达后创建新 Runner；
7. 直到产生语义结果或触发平台限制。

`read_file`、`execute` 和 `invoke_agent` 都必须显式存在于 Tool 快照。

所有 Tool 调用先持久化 ToolInvocation。平台只在同一 Invocation、相同参数和幂等键内重试瞬时技术失败。外部副作用结果无法确认时返回 `outcome_unknown`，不能盲目重放。

访问 Session Workspace 时使用整体读写锁，详见 [Sandbox 与 Artifact](sandbox-artifact.md)。

## AgentInvocation

`invoke_agent` 统一采用“有请求就必须有定向结果”的语义。调用方当前 Runner 结束，Work Item 进入 `waiting_agent`。

同一 Runner 可以并行发起多个 AgentInvocation。平台采用 all-settled：

- 等待本轮全部调用进入终态；
- 单个失败不自动取消其他调用；
- 全部结算后创建新的调用方 Runner；
- Agent 看到每个调用的最终状态和结果；
- 业务上再次调用会创建新的 AgentInvocation，不属于平台技术重试。

Chat 和 Plan 使用严格星形授权；Group 允许图内任意节点互调。模式规则分别见 [Plan 模式](plan-mode.md) 和 [Group 模式](group-mode.md)。

## Clarification 与对话

Clarification 是持久化等待对象，不是图节点。用户通过稳定 `clarification_id` 回答。

- Chat：当前 Agent 直接询问用户；
- Plan：子 Agent 把澄清需求返回 Owner，只有 Owner 询问用户；
- Group：节点 Agent 直接向用户澄清，回答只进入提问节点的直接可见范围；其他节点需要显式查询共享记录。

Clarification 默认没有业务超时，用户离线后 Work Item 保持 `waiting_user`。用户主动消息按消息作用域路由到普通 UserSession 或 GroupSession，不能依赖页面外观推断目标。

## 技术超时与崩溃恢复

Runner、模型请求和 ToolAttempt 可以具有平台技术超时。技术超时结束当前尝试，不必直接终止 Work Item；符合条件时平台创建新 Attempt 或 Runner 恢复。

Runner 只承诺至少一次调度，不承诺进程只启动一次。同一 Work Item 同时最多有一个 active Runner，Worker 使用租约与 fencing token 防止重复推进。ToolInvocation 是副作用恢复边界：

- 已成功调用直接复用持久化结果；
- 具有幂等键的调用可以查询或安全重试同一次 Invocation；
- 结果未知且不支持幂等的外部操作进入 `outcome_unknown`；
- Agent 只看到最终成功、失败、取消或结果未知。

`invoke_agent` 和持久化异步 Tool 等待期间不占用 Runner。普通闲置不会让 Plan、GroupRun 或 Session 自动失败。

## 状态、收敛与取消

平台负责合法状态迁移和持久化，Agent 表达语义结果但不能直接写执行数据库。

取消采用尽力而为：

```text
cancellation_requested
→ 停止新 Runner / ToolInvocation / AgentInvocation
→ 尝试取消运行中操作
→ 按真实结果结算
```

已经完成的业务副作用不回滚。用户离开页面不触发取消。Plan、GroupRun 和 GroupSession 的取消范围由各模式模块定义。

未完成项可以阻止 Run 宣称 `succeeded`，但不能阻止结算。用户或负责最终交付的 Agent 可以要求收敛：

```text
停止创建新工作
→ 尽力取消运行中的 ToolInvocation / AgentInvocation
→ 将未完成 Work Item 标记为 cancelled 或 abandoned
→ 保留已有结果与 Artifact
→ 结算为 completed + partially_succeeded / failed，或 cancelled
```

迟到的外部结果记录为新 ExecutionEvent，但不能重新激活终态 Run。

## Runner 快照

每个 Runner 至少记录：

```text
agent_id
agent_version_id / agent_version
execution_principal
model_id / runtime_identifier / parameter_version
sandbox_profile_id / sandbox_version
tool_keys[] / tool_versions[]
skill_id / version / checksum / path[]
四类 policy key / version
agent_catalog[]
prompt_section_sources[]
session / plan / group / run / work_item identifiers
parent_invocation_id
referenced_event_ids[]
status and timestamps
```

正式文件以轻量 Artifact 引用记录，文件本体仍由 Session Workspace 管理。Runner 快照、业务状态和 Observability Span 职责不能混合。

## 更新、等待与重跑

Agent 的版本选择遵循：

- Chat 每个新 Run 使用当时最新发布 AgentVersion；
- Plan 和 Group 使用用户确认图时固定的参与 AgentVersion；
- 正在运行的 Runner 永远保持启动快照；
- Agent 停用阻止新 Run，不强行中断已经开始的 Run。

AgentVersion 中引用的 Tool、Model 和 Policy 仍受平台注册治理：Tool 或 Model 被停用时后续实际调用明确失败，不能因为历史版本曾选择而绕过当前平台治理。Tool Confirmation Policy 按执行前的当前值执行。

Skill 采用独立热更新语义：AgentVersion 固定 `skill_id`，每个新 Runner 解析当前有效 SkillVersion 并固定 checksum。因此同一长执行中的后续 Runner 可以使用已经发布的 Skill 新版。Sandbox 在 Session 创建时固定，不随 Agent 后续配置切换。

等待条件满足时创建新 Runner，不恢复旧进程。从终态历史重新执行时创建新 Run，并可以记录 `source_run_id` 或 `source_work_item_id`，但解析新 Runner 启动时的有效配置。

第一阶段支持 Checkpoint 恢复和重新执行，不支持任意 Fork 或确定性 Replay；相关能力放在项目后期。
