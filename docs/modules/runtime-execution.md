# Runtime 执行

本文是 MyClaw 通用 Agent Loop、模型协议、能力、Action、停止、等待、恢复和结果验证的唯一权威来源。

## Runtime 的职责边界

Runtime 负责让 Agent 稳定地观察环境、调用模型、执行行动并继续工作：

```text
Model owns
理解 / 规划 / 行动选择 / 协作选择 / 专业判断 / 最终表达

Runtime owns
上下文装配 / 权限 / Tool协议 / 持久状态 / 副作用 / 等待 / 恢复 / 预算 / 观测
```

Runtime 不是业务工作流引擎，不根据预设 Plan、Group 角色或完成清单选择下一步。

## 通用执行对象

Agent 本身不属于执行对象。`Agent` 是持久逻辑身份，`AgentVersion` 是不可变配置蓝图；以下对象描述某次配置如何在具体用户、上下文和权限下被运行。

### UserSession

持续对话和工作上下文。保存入口 Agent、消息、模式、WorkingPlan、Workspace 和关联 Run。Session 可以长期存在，不代表一直有进程运行。

### Run

一次用户请求、后续追问或 Agent 协作工作：

```text
active
waiting_user
waiting_approval
waiting_tool
waiting_agent
completed
cancelled
failed
```

Run 终态后不重新激活。后续输入创建新 Run 并引用历史。

### WorkItem

可以跨 Runner 和等待持续的工作容器。Chat 根请求、AgentInvocation、Group 成员工作或后台验证都可以对应 WorkItem，但它不是固定流程节点。

### Runtime Worker

实际承载代码、模型调用和 Tool 协调的长期服务进程。Worker 可以先后运行不同 Agent 的 Runner；它不是 Agent，不保存 Agent 的权威身份或长期工作状态。

### Runner

某个 Runtime Worker 对一个 WorkItem 的短生命周期连续执行片段。Runner 获得 lease 和 fencing token，加载 AgentVersion、Context 与 CapabilitySnapshot，运行若干完整 AgentTurn，直到：

- 需要持久等待；
- 当前 Turn 自然结束并完成 WorkItem；
- 达到 Runner 技术限制；
- 被取消或失败。

等待满足后，调度事务创建新 queued Runner 与 Outbox；任意合格 Runtime Worker 都可以领取，不恢复旧进程，也不要求回到原机器。

因此不能表述为“Agent 有或没有 Runner”。准确语义是：WorkItem 可运行时调度 Runner；Agent、Session 和 WorkItem 在没有 Runner 时仍然存在。

### AgentTurn / ProviderAttempt

AgentTurn 是一次逻辑模型决定；ProviderAttempt 是传输层尝试。只有零 payload、未产生 Tool Call 和未提交副作用的可重试错误，才允许透明重试同一 Turn。

## ExecutionPrincipal 与 CapabilitySnapshot

每个 Runner 固定模型不可修改的执行身份：

```text
tenant_id
account_id
authentication_source: web_session | agent_api_key | oidc_user | oidc_obo | service_principal
api_key_id?
external_identity_id?
asserted_user_id_snapshot?
credential_revision
account_authorization_revision
permission_revision
user_session_id
run_id / work_item_id
entry_agent_id
current_agent_id
group_session_id?
parent_agent_invocation_id?
delegation_chain[]
```

有效能力为：

```text
AgentVersion assembly
∩ original principal authority
∩ current permission mode
∩ collaboration / membership boundary
∩ platform and tool governance
∩ hard budget
```

`CapabilitySnapshot` 至少记录：

```text
assembled_tool_keys[]
effective_tool_keys[]
denied_capabilities[] + reason_code
discoverable_agents_scope
callable_agents_scope
permission_mode
governance_versions
budget_snapshot
runtime_release_id
harness_policy_versions
provider_adapter_version
tool_schema_bundle_hash
experiment_cohort?
```

Provider 看到的 Tool Definitions 必须与 effective 集合一致。自然语言、Skill 文本、角色名称和历史消息不能改变权限。

Tool 成功一次不会从后续能力中消失。只有机器态权限、模式、治理、预算或装配变化时，新 Runner 才重新解析 Snapshot。

AgentVersion 只冻结 Agent 装配，不冻结整个平台 Harness。每个 Runner 还必须记录实际 Runtime release、Context/Compaction/Continuation Policy、Provider Adapter 和 Tool Schema bundle 版本，才能解释同一 AgentVersion 在平台升级前后的行为差异。

## Context Package

每个 AgentTurn 动态装配最小高信号上下文：

```text
1. Platform Runtime Rules
2. AgentVersion Instructions
3. Current permission / budget / environment summary
4. Effective Tool definitions
5. Relevant Skill content
6. Current user or agent input
7. WorkingPlan current projection
8. Unconsumed Tool / Agent / Clarification / Approval results
9. Selected history and memory
10. Group scoped messages / SharedTasks when applicable
```

每段保留来源、版本、checksum、visibility 和 token estimate。Context Assembler 应：

- 优先当前目标、最近输入、失败和未消费结果；
- 用稳定引用代替重复的大型 Tool 输出；
- 在 Compaction 前生成可恢复的工作摘要；
- 保留 WorkingPlan、未完成 Action、权限和关键决定；
- 不注入其他 Agent 的 Instructions、私有 Memory、凭证或 Chain-of-Thought。

## Provider 最小协议

目标 Provider 只需可靠支持以下通用语义：

```text
ordered turn_items[]
  ├── assistant_message(content_parts[])
  └── tool_call
tool_result input
stop_reason
usage
stream events
errors
provider_state_ref?
```

第一阶段内容 part 至少支持 text、file/artifact reference 和 structured data；不支持的模态必须明确报错。Adapter 把不同供应商 wire format 映射到有序 Turn Item，不要求所有 Provider 生成整段结构化最终 JSON。供应商维持连续推理所需的不透明状态可以保存为受控引用，但不能被解释成 MyClaw 业务状态或共享 Chain-of-Thought。

### 普通最终消息

无 Tool Call 的 assistant message 可以自然结束当前 Turn。Runtime 不要求额外 `finalize_response`。

### Tool Call

每个 Tool 只校验自己的小型参数 schema。Runtime 先验证能力和参数，再创建 Invocation，随后执行或进入 Approval/异步等待。

### Stop Reason

Stop reason 只说明 Provider 为什么结束当前采样，例如正常结束、Tool Use、长度、内容过滤或错误。它不是业务成功状态。

### 结构化输出

只有真正需要机器对象的局部能力使用结构化 Tool 参数，例如更新计划或邀请 Agent。普通对话、最终汇报和 Group 总结不强制结构化 Completion Contract。

## Runtime Coordination Tools

Runtime 可以向模型提供少量真实协调能力：

| Tool | 真实作用 |
| --- | --- |
| `update_plan` | 更新当前 Agent 的 WorkingPlan 投影 |
| `ask_user` | 创建持久 Clarification |
| `discover_agents` | 查询授权范围内 AgentCard |
| `invoke_agent` | 创建定向 AgentInvocation |
| `invite_agent` | 创建 Group Membership 邀请或 Approval |
| `send_group_message` | 写入定向或 Group Message |
| `update_shared_task` | 更新可选 Group 协作白板 |

这些不是业务系统 Tool，但也是有权限、有结果、可审计的真实 Action。它们不能：

- 关闭整个 Run；
- 声明任务成功；
- 定义必需 Tool 清单；
- 自动扩大 Tool 或数据权限；
- 用调用次数替代业务验证。

不存在 `finalize_response`、`complete_task` 或同义控制 Tool。

## Agent Loop

```text
load authoritative state
→ assemble context + capability snapshot
→ call model
→ persist complete AgentTurn
→ if tool calls:
     validate each call
     create Invocation
     execute inline or enter durable wait
     append authoritative result
     continue
→ else if assistant message:
     route message by WorkItem purpose
     run structural continuation check
     complete, wait, or continue
→ checkpoint at safe boundaries
```

规则：

1. 模型 Action 只有经过 Dispatcher 校验和持久化才生效；
2. 模型文本中的 JSON、XML 或“已经执行”不是 Tool 调用；
3. Tool 结果原样成为下一次模型可观察事实；
4. 模型可以重复使用 Tool、修改计划或重新协作；
5. Runtime 不根据计划 item、关键词或角色名产生隐式 Action；
6. 部分 streaming delta 不是权威消息，完整 Turn 才写安全 Checkpoint；
7. 每个 WorkItem purpose 只决定消息路由，不更换 Agent Loop。

## WorkItem purpose 与消息路由

| purpose | 无 Tool Call 的最终消息如何处理 |
| --- | --- |
| `user_request` | 交付用户，完成本次根 WorkItem |
| `plan_proposal` | 展示方案并进入 waiting approval |
| `agent_request` | 完成 AgentInvocation，结果返回调用方 |
| `group_member_work` | 写入 direct/group 消息，成员 WorkItem 完成或空闲 |
| `background_check` | 保存检查结果并通知父 WorkItem |

路由由创建 WorkItem 时的可信字段决定，不能从模型文字关键词推断。

## Turn、WorkItem、Run 与验收

必须区分四层：

1. **Turn stop**：模型当前采样结束；
2. **WorkItem terminal**：当前工作已返回结果、失败或取消；
3. **Run terminal**：本次用户或协作请求没有继续推进的工作；
4. **Acceptance**：外部业务或 UAT 标准接受结果。

模型普通最终消息通常足以结束一个 WorkItem，但不能覆盖机器事实：如果当前 WorkItem 已经创建必须等待的 Invocation，它应进入 waiting；如果外部操作失败，Run 可以完成并报告失败，不能显示为 verified success。

## Structural Continuation Check

模型准备自然停止时，Runtime 只检查确定性结构状态：

- 本 WorkItem 是否存在 running 或 waiting Invocation；
- 是否存在未回答 Clarification 或 Approval；
- 是否收到用户取消；
- 是否达到硬预算或协议错误；
- 当前交付动作是否受确定性发布 Gate 阻止。

处理方式：

- 有外部等待：保存消息并进入对应 waiting；
- 有同步结果尚未反馈模型：继续下一 Turn；
- 无 pending：按 WorkItem purpose 完成；
- 硬预算或取消：停止新 Action，明确结束原因；
- 发布 Gate 不通过：拒绝发布并把精确事实反馈 Agent。

Continuation Check 不评价文本“够不够好”，不调用通用 Evaluator，也不发明下一业务步骤。需要质量复核时，应显式调用评审 Agent、测试 Tool 或人工流程。

## ToolInvocation 与 ToolAttempt

一次模型 Tool Call 创建一个语义 ToolInvocation；物理执行、查询结果或技术重试形成 ToolAttempt。

```text
requested
waiting_confirmation
queued
running
succeeded
failed
rejected
cancelled
outcome_unknown
```

技术重试只复用同一 Invocation，并满足：

- 参数和 operation key 相同；
- Tool 声明该失败可重试；
- 不会重复不可确认副作用；
- 尝试次数在预算内。

模型基于新事实再次调用同一 Tool 是新的业务 Invocation。以下不能作为全局去重依据：

- Tool key 相同；
- 文件 checksum 相同；
- 同一个 Agent 曾经调用过；
- 文本看起来类似。

Tool 需要声明 idempotency scope、可查询结果能力和未知结果策略。

## AgentInvocation

AgentInvocation 保存：

```text
caller / target AgentVersion
objective and input message
context refs and visibility
permission / budget snapshot
status and timestamps
final message / artifact refs / error
parent invocation and depth
```

调用方在需要结果时进入 `waiting_agent`。目标 Agent 正常最终消息完成 Invocation；失败、拒绝、取消和超时也作为明确结果恢复调用方。

Chat/Plan 委派结果回调用方；Group 成员还可以直接写入 Group 消息。Agent 调用保持 Run 创建时固定的 `tenant_id + account_id`，不能通过目标 Agent 改写身份或提权。Agent API 来源另保留 `api_key_id + asserted_user_id` 审计证据；后者是调用方裸断言而非终端用户证明，不再作为领域授权键。

同一 MyClaw 控制域内，AgentInvocation 不建立目标 Agent 进程的网络连接：

```text
CollaborationService 校验调用
→ AgentDirectoryService 从数据库解析目标 AgentVersion
→ 调度事务创建目标 WorkItem、queued Runner 与 OutboxCommand
→ Runtime Worker 领取 Runner
→ Runner 加载目标配置并执行同一个 Agent Loop
→ 结果完成 Invocation，按可信 return route 恢复调用方
```

Delegation 与 Group 复用这条路径；Group 只额外校验 Membership、visibility 和共享上下文。未来远程目标由 Agent Gateway 适配为外部 Task 协议，本地 Invocation 及审计模型保持不变。

## Clarification 与 Approval

Clarification 用于缺失信息或用户判断，Approval 用于精确副作用、Plan 实施或成员加入等权限变化。二者都是持久等待对象，具有稳定 ID、请求者、作用域和版本。

用户回答或批准后：

1. 事务提交结果和 ExecutionEvent；
2. Outbox 调度同一 WorkItem 的新 Runner；
3. 新 Context 明确包含问题、回答/决定和相关 Action；
4. 旧 Runner 不恢复，迟到写入被拒绝。

不能把 Tool Confirmation 当成通用业务审批，也不能把模型问句字符串解析成 Clarification。

## Plan 与 Group 在 Runtime 中的实现

### Plan

Plan 只增加：

- `permission_mode=plan_readonly`；
- WorkingPlan 始终可见；
- 最终方案消息进入 `waiting_approval`；
- 批准后原 Session 恢复可执行 permission mode。

没有独立 Plan DAG 调度器。

### Group

Group 只增加：

- Membership 与 RunSnapshot；
- 可寻址成员目录；
- direct/group Message；
- SharedTask 可选投影；
- Group 级预算和可见性。

每个成员仍运行相同 Agent Loop。没有 Group Supervisor Runtime。

## 结果事实与验证

Runtime 保存事实，不要求模型提交通用 OutcomeClaim：

- ToolInvocation 与 stdout/stderr/业务响应；
- 实际 argv、退出码和测试报告；
- Workspace 文件与 checksum；
- Artifact 发布和下载信息；
- Agent 消息、失败和异议；
- 用户 Approval 和 Acceptance。

产品可以按场景注册 `VerificationRule`，例如精确九例。Rule 只能读取权威事实并产生 `passed | failed | insufficient_evidence`，不能调用业务 Tool 或改变模型计划。

独立 LLM Evaluator 是可选调用，适用于文本质量、复杂评审或开放标准；其输出作为一条评审事实返回 Agent/用户，不拥有通用结算权。

## Budget 与 no-progress

Run 累计：

```text
agent_turns / provider_attempts
tool_invocations / agent_invocations
tokens / cost / active_time
fan_out / depth / concurrency
structured_repairs / transport_retries
```

等待用户、Approval 和持久异步资源不计算 active execution time。达到软阈值时，把剩余预算反馈模型；达到硬阈值时停止新 Action，并让 Agent基于已有事实汇报或由 Runtime 明确失败。

no-progress 只能作为诊断和反馈：连续多个 Turn 没有新消息、计划更新、Invocation 结果、文件变化或共享事实时，要求 Agent反思或找人。它不能替 Agent 宣布业务失败，也不能无限续跑。

## 取消与失败

```text
cancellation_requested
→ stop new turns and invocations
→ best-effort cancel running attempts
→ preserve completed side effects
→ record late results without reactivation
→ terminal state
```

Provider、协议、Tool、权限、资源和业务失败使用不同 reason code。Runtime 不能用换模型、删除 Tool、忽略 schema 或伪造 fallback 来掩盖配置错误。

## Checkpoint 与恢复

安全 Checkpoint 至少包含：

- Run/WorkItem/Runner 版本和 epoch；
- AgentVersion 与 CapabilitySnapshot；
- Context manifest 与 Compaction 引用；
- WorkingPlan revision；
- 已消费和未消费结果游标；
- pending Clarification/Approval/Invocation；
- Group Membership/Message cursor；
- 累计预算和取消状态。

Worker lost 后，新 Runner 从权威数据库和 Workspace 构建新 Context。已经成功的 Invocation 只引用结果，不重复执行；未知副作用保持 `outcome_unknown`。

## 可观测性

每个 Runner 至少能解释：

- 为什么选择当前 AgentVersion、Model 和 permission mode；
- 模型实际看到了哪些 Tool 和 Agent；
- Context 由哪些来源组成；
- 每个 Turn 的消息、Tool Call、stop reason 和 usage；
- Action 为什么允许、拒绝、等待或失败；
- WorkingPlan 如何变化；
- Group 成员如何发现、邀请和通信；
- 为什么当前 Run 在工作、等待或终止；
- 哪些结果 observed、verified、reviewed 或 accepted。

Trace 不作为恢复或业务事实源，也不存凭证和原始 Chain-of-Thought。

## 禁止设计

- 模型可调用的 `finalize_response` 或同义 Tool；
- 模型声明 `required_tool_keys`；
- Tool 类型一次成功后被消费；
- 普通最终消息强制嵌套复杂 JSON；
- 从文本关键词推断 Tool、Clarification、完成或下一步；
- Chat、Plan、Group 三套模型循环；
- 根据 Plan/SharedTask 自动创建业务调用；
- 通用 `OutcomeClaim + Evaluator` 结算链；
- 未知结果上的盲重试；
- 用 Trace 或缓存替代 MySQL 业务事实；
- 把 Agent、AgentVersion、WorkItem、Runner 或 Runtime Worker 合并成一个生命周期；
- 为本地 Delegation 和 Group 复制两套 Agent 调用内核；
- 让本地 Agent 通过进程直连绕过 AgentInvocation、CapabilitySnapshot 和 Outbox。
