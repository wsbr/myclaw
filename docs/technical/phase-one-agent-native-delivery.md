# 第一阶段 Agent-native 交付技术方案

> 状态：目标设计，尚未表示代码完成。本文是 `vibe_2` 第一阶段架构、迁移、测试和 UAT 门禁总纲；详细数据、状态与 API 权威见本文链接。

## 1. 文档目标

本方案把三模式从“平台编排三个流程”改造为“一个 Agent Loop 上的三种策略”：

```text
Chat  = Agent Loop + direct execution policy
Plan  = Agent Loop + plan_readonly / approval policy
Group = Agent Loop + network membership / messaging policy
```

一期交付不是增加更多完成合同、状态机和治理对象，而是删除会压制模型能力的错误 Harness，把已经正确的可靠执行能力放回最小 Agent 架构中。

产品语义以以下文档为准：

- [设计哲学](../DESIGN_PHILOSOPHY.md)；
- [第一阶段交付边界](../modules/phase-one-scope.md)；
- [Plan 模式](../modules/plan-mode.md)；
- [Group 模式](../modules/group-mode.md)；
- [Runtime 执行](../modules/runtime-execution.md)；
- [Agent-native Harness 调研](agent-native-harness-research.md)。

V1 补丁阶段曾使用 `finalize_response`、模型声明 `required_tool_keys`、Tool 类型一次性消费和冻结 Plan DAG；这些设计已失效，不得进入新实现。旧方案只通过 Git 历史和对应验收报告追溯，不再保留在活动设计文档树中。

## 2. 架构决策摘要

| 主题 | 第一期决策 |
| --- | --- |
| Agent Runtime | Chat、Plan、Group 共用一个模型—Tool Loop |
| 普通 Chat | 复杂任务由模型自主决定是否生成 WorkingPlan |
| Plan | `permission_mode=plan_readonly`，批准后原 Session 继续执行 |
| WorkingPlan | 可在任何模式使用；只做模型工作记忆和 UI 投影 |
| Group 成员 | 用户从 Agent Network 直接选择真实 Agent |
| Group 模板 | 不存在核心模板、角色槽位和 Formation JSON |
| 第一期成员 | 产品、开发、测试本次 GroupRun 快照固定 |
| Provider 输出 | 普通 Assistant Message + native Tool Call + stop reason |
| 最终响应 | 不要求复杂结构化 Completion Envelope |
| 完成 | 自然 Turn Stop + 机器 pending 状态 + Run lifecycle |
| 验证 | 精确 Tool/文件/测试规则；LLM Evaluator 非通用前置 |
| Tool 生命周期 | 授权不变时持续可见，按具体 Invocation 治理副作用 |
| 恢复 | 复用现有 Outbox、lease/fencing、Checkpoint 和 Invocation |
| UAT | 固定场景与数据，不固定计划和协作路径 |

## 3. 当前故障与目标修复

2026-08-04 UAT 中，Chat 通过，Plan 和 Group 业务闭环失败。根因链与目标处置如下：

| 当前故障 | 根因 | 目标处置 |
| --- | --- | --- |
| 最终回合 structured contract 失败导致 WorkItem failed | 把普通消息强制变成复杂 JSON | 普通 final message 自然结束；只校验 Tool 参数 |
| `finalize_response` 与业务 Tool 竞争 | 完成被建模为模型 Tool | 删除完成 Tool，以 stop reason + message 收口 |
| 模型声明 `required_tool_keys` | 工作者同时定义验收合同 | 删除字段，机器验证直接读取权威执行事实 |
| Tool 成功一次后消失 | Tool 类型被误当成一次性效果 | Tool 持续可见；具体副作用按 Invocation 幂等 |
| Plan 冻结六节点 DAG | 初次理解被当成执行工作流 | WorkingPlan 动态更新，平台不自动调度节点 |
| Plan 卡在 Agent/User/Confirmation 等待 | 状态分散且取消不闭环 | 统一等待对象、恢复规则和 Run 取消事务 |
| Group 要求 Formation JSON | 组队被建模为一次结构化生成 | UI/Agent Network 直接选人并创建 Membership |
| 九例只跑 6/9 但模型声称通过 | 完成文本与环境事实脱节 | VerificationRule 检查实际 argv/stdout/exit code |
| 相同文件被重复发布 | checksum 被错误等同业务幂等 | Artifact publish 使用目标路径+意图+operation key |
| 图外拒绝没有 AgentInvocation 审计 | 在 capability 解析前静默阻止 | 记录 rejected invitation/invocation 与 reason code |
| Provider policy 与版本装配不一致 | Snapshot 与实际 Tool 暴露漂移 | 每个 Runner 固定并展示 CapabilitySnapshot |

## 4. 范围与非目标

### 4.1 第一期必须完成

- 一个统一 Agent Loop；
- Model Adapter Conformance Suite；
- AgentTurn / ProviderAttempt 可观测关系；
- 稳定 CapabilitySnapshot；
- 普通 Assistant Message 自然结束；
- Tool/Agent/Clarification/Approval 持久等待与恢复；
- 任意模式可用的 WorkingPlan；
- `plan_readonly` 和用户批准；
- Agent Network 选择成员；
- Group Membership、消息和 RunSnapshot；
- 精确 VerificationRule；
- 三模式 UAT、稳定性和故障注入；
- 旧 UAT 数据只读保留。

### 4.2 明确非目标

- Group 动态自动加人；
- Group Template / Role Slot；
- Charter Revision / Goal Contract 平台；
- 全局 OutcomeClaim / Evaluator；
- 生产级不可信代码隔离；
- 外部知识库、订单、退款等业务系统；
- MCP 和跨组织 Agent 联邦；
- Kubernetes HA、外部 MQ；
- 任意时间点确定性 Replay；
- 自动修改 AgentVersion 或 Skill。

## 5. 目标物理架构

```text
Browser / API Client
        │ HTTP + SSE
        ▼
API
├── Session / Run commands
├── Plan permission and WorkingPlan
├── Agent Network / Group membership
├── Clarification / Approval
└── Artifact / Verification query
        │
        ▼
MySQL + Database Outbox ─────► Runtime Worker
        │                       ├── Snapshot Resolver
        │                       ├── Context Assembler
        │                       ├── Model Adapter
        │                       ├── Agent Loop
        │                       └── Action Dispatcher
        │                                  │
        │                    ┌─────────────┴─────────────┐
        │                    ▼                           ▼
        │              Tool/Sandbox Worker       Agent WorkItem
        │                    │                           │
        └◄── Event/Result ───┴──────── Outbox/Event ────┘

Redis: provisional token bridge only
OTel: diagnostics only
Filesystem: Workspace / Skill / Artifact bodies
```

不增加独立 Plan Worker、Group Supervisor Worker 或 Outcome Evaluator Worker。所有模式在 Runtime Worker 中进入同一 `AgentLoopCoordinator`。

### 5.1 Agent 从配置到运行

第一阶段不把 Agent 部署成常驻服务，也不给每个 Agent 分配独立网络地址：

```text
Agent        = 稳定逻辑身份、Owner 和治理边界
AgentVersion = 不可变 Model / Instructions / Tool / Skill / Policy / Sandbox 配置
WorkItem     = 某次请求或调用的持久工作状态
Runner       = Runtime Worker 对 WorkItem 的一段连续执行
```

用户消息、AgentInvocation 或定向 GroupMessage 创建或恢复 WorkItem。调度事务创建 queued Runner、更新 WorkItem 推进权并写 Outbox；任意合格 Runtime Worker 取得 Runner lease 后加载目标 AgentVersion、Context 和 CapabilitySnapshot。等待时 Runner 结束，条件满足后由新的 queued Runner 继续同一 WorkItem。

同一个 AgentVersion 可以并发产生多个 Runner，它们仍属于同一 `agent_id`。只有显式创建不同 Agent 身份和数据边界，才表示不同 Agent。

## 6. 组件职责

| 组件 | 负责 | 不负责 |
| --- | --- | --- |
| `SnapshotResolver` | AgentVersion、身份、权限、模式、成员、预算快照 | 根据 Prompt 补能力 |
| `ContextAssembler` | 高信号 Context、历史选择、Compaction | 业务规划和结论 |
| `ModelAdapter` | Provider wire protocol、stream、Tool Call、stop reason | 权限、业务结算、隐式换模型 |
| `AgentLoopCoordinator` | Turn→Action→Observation 循环 | 预定义业务步骤 |
| `ActionDispatcher` | 参数、能力、Approval、Invocation 持久化 | 从模型文字推断 Action |
| `WorkingPlanService` | revision、CAS、事件和 UI 投影 | 自动执行 plan item |
| `CollaborationService` | 发现、邀请、AgentInvocation、消息 | 选择业务协作者 |
| `AgentDirectoryService` | 从本地 AgentCard 投影做精确解析和语义查询 | 直接授权调用、运行 Agent |
| `RunLifecycleService` | waiting、resume、cancel、terminal | 评价文本质量 |
| `VerificationService` | 场景化确定性检查 | 调用 Tool 修复结果 |
| `ArtifactService` | 发布、checksum、版本、下载 | 用 checksum 推断全部业务幂等 |
| `EventService` | 可靠业务时间线和 SSE | 以 Trace 代替事实 |

## 7. 最小数据模型

本节只给出实体地图。字段、主外键、唯一约束和索引以[第一阶段数据库契约](database-schema.md)为唯一实施依据；状态枚举和合法迁移以[状态机](state-machines.md)为准。本节与二者冲突时必须修订本节，不能由实现自行选择。

原则：优先复用现有 `Run`、`WorkItem`、`Runner`、`ToolInvocation`、`AgentInvocation`、`Clarification`、`Artifact`、`ExecutionEvent` 和 Outbox；代码类可以保留 `DurableRunner` 名称，但领域文档统一称 `Runner`。只增加无法稳定表达的新事实。

### 7.0 Agent 与版本解析

现有 `agents` 保留稳定身份、Owner、状态和 `current_published_version_id`；`agent_versions` 保存 `draft|published|deprecated|draft_test_snapshot` 版本及完整依赖快照。Publish 在校验通过后以 CAS 原子更新当前版本指针，并生成对应 AgentCard 投影。发布关系必须把逻辑 Skill 解析为精确 `skill_version_id + checksum`；Runner 不追随 Skill 的 current pointer。

普通 Chat/Plan 委派在创建 AgentInvocation 时解析并固定目标当前发布版本；Group 内调用使用 GroupRunSnapshot 中的目标版本。停用或新发布只影响之后的解析，不改变既有 Run、WorkItem 和 Invocation。

### 7.1 UserSession

核心字段摘要：

```text
permission_mode: direct | plan_readonly
permission_revision
```

Run 的 `mode=chat|plan|group` 影响 UI 和消息路由；Session 的 `permission_mode` 进入 CapabilitySnapshot。GroupSession 是关联资源，不把 Session 永久切成 group 字符串状态。

### 7.2 Run

统一状态：

```text
status:
  active | waiting_user | waiting_approval |
  waiting_tool | waiting_agent |
  completed | cancelled | failed

terminal_reason:
  model_final | user_cancelled | budget_exhausted |
  provider_error | protocol_error | tool_error |
  policy_denied | superseded | other
```

`completed` 表示本次请求已经有明确交付，不自动等于业务 verified。验证状态单独投影。

### 7.3 AgentTurn

新增关系表：

```text
agent_turns
- id
- runner_id
- turn_no
- agent_version_id
- capability_snapshot_id
- context_manifest_json
- status: started | waiting_actions | completed | failed
- assistant_message_id?
- turn_item_manifest_json
- provider_state_ref?
- provider_stop_reason?
- usage_json
- started_at / completed_at
```

同一 Runner 的 `turn_no` 唯一。只有完整 Turn 成为安全 Checkpoint 候选。

### 7.4 ProviderAttempt

```text
provider_attempts
- id
- agent_turn_id
- attempt_no
- model_id / provider
- request_hash
- first_payload_at?
- status: queued | running | succeeded | failed | timed_out | outcome_unknown
- transport_error_code?
- usage_json?
- started_at / completed_at
```

透明重试只允许在前一 Attempt 没有 payload、Tool Call 和副作用时发生。

### 7.5 WorkingPlan / PlanUpdate

```text
working_plans
- id
- session_id
- run_id?
- owner_agent_id
- revision
- status: active | superseded | closed
- created_at / updated_at

plan_updates
- id
- working_plan_id
- revision
- explanation?
- plan_json: [{text, status}]
- source_turn_id
- created_at
```

约束：`(working_plan_id, revision)` 唯一；CAS 更新；旧 revision 不删除。Plan item 没有自动调度外键。

### 7.6 CapabilitySnapshot

现有 Runner Snapshot 扩展或关系化：

```text
capability_snapshots
- id
- runner_id
- tenant_id / account_id
- authentication_source
- external_identity_id? / api_key_id? / asserted_user_id_snapshot?
- credential_revision / account_authorization_revision / permission_revision
- agent_version_id
- permission_mode
- assembled_tool_keys
- effective_tool_keys
- denied_capabilities_json
- discover_scope_json
- callable_scope_json
- governance_versions_json
- budget_snapshot_json
- runtime_release_id
- harness_policy_versions_json
- provider_adapter_version
- tool_schema_bundle_hash
- experiment_cohort?
- created_at
```

每个 Runner 只能有一个最终 Snapshot。Tool Definition 必须直接由 `effective_tool_keys` 生成。AgentVersion 不承担平台 Harness 版本；Snapshot 必须记录实际 Runtime、Context/Compaction/Continuation Policy、Provider Adapter 和 Tool Schema bundle，保证平台升级后的执行仍可解释。

### 7.7 ToolInvocation / ToolAttempt

保留现有对象，补充：

```text
operation_key
idempotency_scope
target_fingerprint?
request_hash
outcome_known: true | false
authorization_snapshot_ref
source_turn_id
```

禁止以 Tool key 或历史成功集合过滤后续 Tool。

### 7.8 AgentInvocation

```text
agent_invocations
- id
- session_id / run_id
- caller_agent_id / caller_agent_version_id
- target_agent_id / target_agent_version_id
- target_work_item_id?
- objective / input_message
- context_refs_json / visibility
- group_session_id? / membership_id?
- parent_invocation_id? / depth
- return_route
- authorization_snapshot_ref / budget_json / deadline
- status: created | queued | running | succeeded |
          rejected | failed | cancelled | timed_out
- final_message_id? / artifact_refs_json? / error_json?
- created_at / completed_at
```

Delegation 与 Group 复用同一张表和调度路径。Group 字段只增加 Membership、共享可见性和结果路由，不创建第二套调用对象。通过校验的本地 Invocation 在调度事务中创建目标 WorkItem、queued Runner、ExecutionEvent 和 OutboxCommand；被拒绝的 Invocation 只保存拒绝事实，不创建 WorkItem。目标 Agent 不需要预先存在常驻 Runner。

`return_route` 由可信调用位置生成，至少支持 `caller_work_item | group_direct | group_shared`；模型只能选择产品允许的协作动作，不能把结果重定向到未授权主体。

### 7.9 Clarification / Approval

Clarification 继续表达信息输入。Approval 统一 Plan 批准、Tool Confirmation 和成员加入确认：

```text
approvals
- id
- session_id / run_id
- requested_by_agent_id
- kind: plan_implementation | tool | membership | session_grant
- subject_type / subject_id
- subject_version
- status: pending | approved | rejected | cancelled
- target_account_id / subject_hash
- decision_message_id? / decided_at
```

Approval 必须绑定精确 subject/version，不能用一次批准覆盖之后变化的 Action。

### 7.10 Agent Network

第一阶段复用 Agent/AgentVersion 并新增或固化 AgentCard 投影：

```text
agent_cards
- agent_id
- agent_version_id
- visibility_scope
- description / domain_tags
- supported_outcomes
- capabilities_summary
- collaboration_summary
- availability / cost / governance labels
- group_enabled
- content_hash
- published_at
```

本地 `AgentDirectoryService` 直接查询该投影。搜索至少过滤原始用户和租户、visibility、发布状态、协作开关、治理策略、环境兼容性和可用性。`group_enabled=true` 只表示可以成为候选，不表示已经可调用或已授权。

### 7.11 GroupSession 与 Membership

复用现有 `group_sessions`，去掉 Formation/Charter 的强制前置语义。现有 GroupAgentNode 迁移解释为 Membership，或建立清晰新表后保持旧表只读：

```text
group_memberships
- id
- group_session_id
- agent_id
- status: invited | joined | declined | cancelled | left | removed
- invited_by_agent_id?
- invitation_reason?
- context_scope_json
- authorization_source
- joined_at / ended_at
```

第一阶段 Group 创建事务直接写入三个 `joined` Membership 和 `group.created` 事件。

### 7.12 GroupRunSnapshot

```text
group_runs
- id / group_session_id
- run_id (unique; lifecycle authority)
- reporter_agent_id
- summary_epoch / delivered_summary_epoch（请求去重由唯一 GroupSummaryRequest 表示）

group_run_members
- group_run_id
- agent_id
- agent_version_id
- capability_summary_hash
- context_scope_hash
- membership_policy: fixed_for_run
```

`(group_run_id, agent_id)` 唯一。运行中不热切换 AgentVersion。GroupRun 不存独立 status/outcome；API/UI 全部读取一对一 Run。Reporter 只承担自然空闲后的最终汇总责任，不获得 Supervisor 权限。

### 7.13 GroupMessage 投影

```text
messages
- id
- group_session_id / group_run_id
- sender_kind: user | agent | runtime
- sender_agent_id?
- visibility: private | direct | group | user
- recipient_agent_id?
- content
- source_turn_id?
- parent_message_id?
- sequence_no
- created_at
```

同一 Session 的 `sequence_no` 单调递增。Group 字段是统一 Message 的可见性与路由投影，不另建一份消息权威；定向消息只调度接收 Agent。

### 7.14 SharedTask（可选）

第一阶段仅在 UI/协作确有价值时实现：

```text
group_shared_tasks
- id
- group_run_id
- title / description
- status
- assignee_agent_id?
- `shared_task_dependencies` 关系行（同 Group、无环，只用于提示/UI）
- `shared_task_result_refs` 关系行（Artifact/Message/Verification 类型化 FK）
- version
```

它没有自动 Outbox 调度触发器；任何 Runner 都由模型真实 Action 或用户消息产生。

### 7.15 VerificationResult

```text
verification_results
- id
- run_id / group_run_id
- rule_key / rule_version / verification_epoch
- status: passed | failed | insufficient_evidence
- observed_facts_json
- missing_facts_json
- `verification_evidence_refs` 类型化关系行（ToolAttempt/Artifact/Workspace checksum/Business fact）
- created_at
```

VerificationService 只读取权威 ToolAttempt、文件和 Artifact，不调用模型或 Tool。

## 8. Provider 协议

### 8.1 目标规范

Adapter 输出：

```text
NormalizedTurnResult
├── items[] (ordered)
│   ├── assistant_message(content_parts[])
│   └── tool_call
├── stop_reason
├── usage
├── provider_state_ref?
└── provider_metadata
```

第一阶段 content part 至少支持 text、file/artifact reference 和 structured data。允许同一响应按顺序包含 commentary message 和 Tool Call；最终可交付消息必须来自完整 assistant message。Provider 的 thinking/reasoning 字段不进入业务 Message 和 Group 共享记录；需要延续供应商会话时只保存不透明 state 引用。

### 8.2 移除的协议

必须删除：

- `COMPLETION_FINALIZE_TOOL_KEY`；
- `NON_REPEATABLE_COMPLETION_EFFECT_KEYS`；
- `required_tool_keys` 解析、合并和 Ledger 对账；
- `tool_envelope.conversation_completion.v1`；
- `structured.conversation_completion.v1`；
- “finalize_response must be the only call”检查；
- 因完成修复而隐藏 `execute/write/publish/history`；
- 最终 plain text 只有 Ledger 非空时才接受的 fallback。

### 8.3 保留的结构化能力

Tool Calling 本身继续使用 JSON schema。`update_plan`、`ask_user`、`invite_agent` 等每个 Tool 保持小而清晰的 schema，并返回精确错误。

不得再要求模型一次输出嵌套目标、角色、计划、结果和完成合同的大 JSON。

### 8.4 Conformance Suite

每个启用 Provider 运行固定探针：

| Probe | 通过条件 |
| --- | --- |
| plain final | 返回完整 Unicode 文本和正常 stop reason |
| single tool | 产生合法 Tool Call，接收结果后继续 |
| repeated same tool | 两次业务调用均可产生，不被类型消费 |
| parallel tools | 顺序/并行语义可明确适配 |
| bad arguments | schema 错误反馈后模型可修正一次 |
| streaming | delta 顺序正确，完整消息可重建 |
| zero-payload retry | 瞬时错误可安全重试且只有一条 Turn |
| payload then error | 不透明重放，记录 failed/interrupted |
| long context | 达阈值后 Compaction/明确错误，不截断事实 |
| tool result correlation | 多 Tool Result 不串线 |

未通过的 Provider 标记 `unsupported`，不得在三模式 UAT 中启用。

## 9. Agent Loop 详细算法

```text
run_work_item(work_item_id):
  claim_queued_runner()
  resolve_agent_version()
  snapshot = resolve_capabilities_and_budget()
  cursor = load_unconsumed_results()

  while runner_budget_available:
      context = assemble_context(snapshot, cursor, working_plan)
      turn = persist_agent_turn_started(context)
      result = call_provider_with_safe_attempt_policy(turn)
      persist_complete_turn(result)

      tool_calls = ordered_tool_calls(result.items)
      messages = completed_messages(result.items)

      if tool_calls:
          persist_nonfinal_messages(messages)
          decisions = validate_and_persist_calls(tool_calls, snapshot)
          if decisions.require_durable_wait:
              checkpoint_and_finish_runner(wait_state)
              return
          cursor += execute_inline_and_collect_results(decisions)
          continue

      if messages contains deliverable assistant message:
          persist_and_route_message(messages, work_item.purpose)
          decision = structural_continuation_check(work_item)

          if decision == complete:
              complete_work_item_and_maybe_run()
              return
          if decision == wait:
              checkpoint_and_finish_runner(wait_state)
              return
          if decision == continue_with_observation:
              cursor += decision.observation
              continue

      fail_turn_protocol_without_replaying_side_effects()
```

### 9.1 有界修复

- Tool 参数 schema 错误：把精确错误反馈模型，最多一次协议修复；
- 普通最终消息不做 JSON 修复；
- 已持久化 Tool Call 后的 Provider 异常不重新调用同一 Turn；
- 修复预算按 Run 累计；
- 修复失败形成 `protocol_error`，保留已经发生的真实 Action。

### 9.2 Structural Continuation Check

检查：

```text
active/running ToolInvocation?
active/running AgentInvocation?
pending Clarification?
pending Approval?
unconsumed synchronous result?
cancel requested?
hard budget exceeded?
deterministic publish gate blocked?
```

不检查：

- 模型是否调用过某种 Tool；
- 文本是否满足通用 LLM 评价；
- WorkingPlan 是否所有 item 都标 completed；
- Group 是否所有成员都说完成；
- 关键词中是否出现“完成”。

## 10. Mode Policy 详细设计

### 10.1 ChatModePolicy

```text
run.mode = chat
permission_mode = direct
working_plan = optional
root_final_message = completes current user request
delegation_result = returns to current agent
```

模型根据复杂度自主调用 `update_plan`。Runtime 不自动切换显式 Plan。

### 10.2 PlanModePolicy

进入 Plan：

1. API 用 CAS 把 Session `permission_mode=direct` 更新为 `plan_readonly`，递增 `permission_revision`，并创建 `Run.mode=plan`；
2. 创建 `plan.permission_entered` 事件；
3. 后续 Snapshot 删除写入和外部副作用能力，保留只读研究能力；
4. WorkingPlan Tool 始终可用；
5. 无 Tool Call 的方案消息使 Run 进入 `waiting_approval`。

批准：

1. Approval 绑定当前 WorkingPlan revision；
2. 第一阶段批准后的目标权限固定为 `direct`，不引入另一组权限级别；
3. 事务更新 Approval、Session permission mode/revision、ExecutionEvent 和 Outbox；
4. 新 Runner 使用同一 AgentVersion、History、Plan 和 Workspace；
5. 实施中 WorkingPlan 可以继续更新。

拒绝或修改保持 `plan_readonly`。取消终结当前 Run，不改变 Workspace。

### 10.3 GroupModePolicy

创建 Group：

1. 用户通过 Agent Network API 选择产品、开发、测试 Agent ID；
2. API 验证三者可发现、可用、允许加入且版本已发布；
3. 单事务创建 GroupSession、三个 joined Membership、GroupRun、RunSnapshot、首条用户消息和 Outbox；
4. 不调用模型生成 Formation；
5. 每个被唤醒成员按相同 Agent Loop 工作。

Group 消息路由：

- @成员：只创建目标成员 WorkItem；
- 用户未指定接收者的 group message：由 initiator agent 先处理，其他成员按需查询；
- Agent 发布的普通 group message：持久化为共享事实，不自动唤醒全体成员；需要对方立即处理时必须定向发送或创建 AgentInvocation；
- Agent direct message：只唤醒接收者；
- AgentInvocation：调用方按等待策略进入 waiting；
- SharedTask 更新：只写事实，不隐式启动 Agent。

一期图外 `invite_agent`：写 `group.membership_invitation_rejected` ExecutionEvent 和协调 Action 结果，reason=`membership_policy_fixed_for_run`；图外 `invoke_agent` 创建 `rejected` AgentInvocation。两者都把精确原因返回模型，不静默忽略，也不加入成员。

### 10.4 本地调用与未来联邦

第一阶段所有 Agent 都在同一 MyClaw 控制域：

```text
discover → AgentDirectoryService / MySQL
invoke   → AgentInvocation + WorkItem + Outbox
execute  → Runtime Worker / Runner
```

后续跨实例或跨组织时，由 Agent Gateway 负责远程发现、Endpoint、身份握手和 Task 协议适配。上层继续创建 AgentInvocation 并使用相同状态、预算、return route 和审计；不得让本地 Agent 通过进程直连，也不得让外部协议成为本地执行前置依赖。

## 11. Tool 与副作用

### 11.1 Tool 可见性

每次 Runner 从 CapabilitySnapshot 生成 Tool Definitions。禁止使用：

```text
effective_tools = assembled_tools - previously_succeeded_tool_types
```

正确逻辑：

```text
effective_tools = authority intersection at runner start
```

### 11.2 业务幂等

Tool 定义声明：

```text
side_effect_kind
idempotency_scope
supports_status_query
safe_retry_error_codes
unknown_outcome_policy
```

示例：

- `read_file`：可重复，只读；
- `execute`：每个模型 Action 是新 Invocation，技术重试只在相同 operation key 内；
- `write_file`：路径+目标版本+operation key，允许 Agent 基于新内容再次写；
- `publish_artifact`：Session+逻辑名称+source version+operation key；checksum 只校验内容；
- `search_group_history`：可重复只读查询；
- `invoke_agent`：每个业务请求独立，技术重试不重复创建目标 WorkItem。

### 11.3 Approval

Approval 卡展示精确 Tool、参数摘要、目标、风险和 subject version。批准后恢复同一 ToolInvocation；参数发生变化必须新建 Approval。

第一阶段允许用户为明确低风险 Tool 设置 Session Grant，但 Grant 是权限策略，不代表业务 Tool 只执行一次。

## 12. Context 与跨窗口持续工作

### 12.1 Context Section Manifest

每个 Turn 记录：

```text
section_key
source_type / source_id / source_version
visibility
checksum
token_estimate
compacted_from?
```

### 12.2 选择优先级

1. 当前用户/Agent 输入；
2. 未处理 Tool/Agent/Approval/Clarification 结果；
3. 当前 permission mode 和有效能力；
4. WorkingPlan；
5. 最近失败、修改和关键决定；
6. 相关 Workspace/Artifact 稳定引用；
7. 精选历史和 Memory；
8. Group 中与当前 Agent 有关的 direct/shared 事实。

### 12.3 Compaction

Compaction 生成可恢复摘要，至少保留：

- 用户目标和已确认口径；
- 当前 WorkingPlan；
- 已完成/失败/unknown Action；
- 未解决问题和 pending 等待；
- 关键文件、Artifact 和测试引用；
- Group Membership、定向消息游标和当前任务；
- 权限、预算和取消状态。

旧 Tool 输出保留数据库/文件引用，默认不重复注入全文。

## 13. 状态、一致性与恢复

完整迁移表、终态、迟到结果和 Group 汇总 epoch 见[第一阶段状态机](state-machines.md)。本节只摘要不变量。

### 13.1 事务边界

以下必须同事务提交：

- 领域状态变化；
- ExecutionEvent；
- 后续 OutboxCommand；
- 乐观版本更新。

### 13.2 Runner 推进权

- 同一 WorkItem 最多一个 active Runner；
- lease + heartbeat + execution epoch + fencing；
- Worker lost 后 maintenance 创建新 Runner；
- 旧 epoch 的 Message、Invocation 和终态写入全部拒绝；
- waiting 状态不占用 Runner。

### 13.3 取消

Run Cancel API：

1. CAS 写 `cancellation_requested_at`；
2. 发 `run.cancellation_requested`；
3. 停止新 Turn/Invocation；
4. 尽力取消 running ToolAttempt；
5. waiting Approval/Clarification 标 cancelled；
6. 保留已完成副作用；
7. 终结 Run 并发 SSE。

UI 取消成功必须能从数据库读取到时间和终态，不能只改变前端状态。

### 13.4 迟到结果

迟到结果写 `late_result.observed` Event，并关联原 Invocation；不得修改终态 Run、重新唤醒 Agent 或覆盖新 epoch。

### 13.5 Group 自然收口

每个减少 pending work 的事务都写幂等 `evaluate_group_quiescence` 命令。Coordinator 确认无 Runner、Invocation、人类动作和明确进行中的 SharedTask，且无 queued/running SummaryRequest 后，在锁内递增 `summary_epoch` 并创建该 epoch 唯一的 Reporter WorkItem；Reporter final 提交且二次结构检查仍为空时，原子更新 delivered epoch 并完成唯一 Run。Reporter 发起新动作时其 WorkItem 在同一 epoch 等待并恢复；交付前出现无关新工作则 supersede 当前 request，收敛后开启下一 epoch。Reporter 不可用时进入 `waiting_user`，用户从本轮 available 成员中替换后，平台在新 epoch 创建新 SummaryRequest/WorkItem，不复活失败记录。

## 14. Verification

### 14.1 与 Runtime 完成分离

Agent 可以自然交付“当前完成了什么、什么失败了”。VerificationService 只决定某个明确断言是否被机器事实证明。

### 14.2 容量计算器规则

`capacity_risk.v1` 读取：

- 被测文件 checksum；
- 每个 execute ToolAttempt 的精确 argv；
- stdout/stderr；
- exit code；
- 执行时间和 Invocation ID；
- 四个 Artifact 的存在与 checksum。

规则逐条匹配 T01—T09，不接受等价数值、参数反转或模型生成的汇总表作为替代。只有 9/9 和四个 Artifact 均通过时返回 `passed`。

### 14.3 可选评审

若未来需要文本质量、架构或合规评审，显式调用 Reviewer Agent 或业务 Tool，结果作为普通 Message/Artifact/VerificationResult。它不成为所有 Run 的隐藏终态服务。

## 15. API

认证、完整资源、请求响应、错误、幂等和权限语义见[API 与 SSE](api-sse.md)，路径与 Schema 以 [OpenAPI 3.1](openapi-v1.yaml) 为准。第一阶段 Agent API 使用绑定入口 Agent 的 Key，并接受 `X-MyClaw-User-Id` 裸断言；Key 持有者可以冒充部署内任意 userId。这是受控 UAT 假设，生产使用 `enterprise_oidc_v1`。

### 15.1 Session 与模式

```text
POST /api/v1/sessions
POST /api/v1/sessions/{id}/messages
POST /api/v1/sessions/{id}/plan-mode
POST /api/v1/sessions/{id}/plan-mode/exit
GET  /api/v1/sessions/{id}
```

切入 Plan 自动设置 `plan_readonly`；Group 是与 Session 关联的协作资源，不把 Session mode 改成第三个可变身份状态。

### 15.2 WorkingPlan

```text
GET  /api/v1/runs/{id}/working-plans
GET  /api/v1/working-plans/{id}/updates
POST /api/v1/working-plans/{id}/approve-implementation
POST /api/v1/working-plans/{id}/continue-planning
```

模型更新通过内部 Runtime Tool，不向用户暴露内部 Tool key。用户 approve 必须带当前 revision。

### 15.3 Agent Network

```text
GET /api/v1/agent-network/cards?query=&domain_tag=&group_enabled=
GET /api/v1/agent-network/agents/{agent_id}
```

搜索默认返回每个 Agent 当前有效发布版本的 AgentCard；按 `agent_id` 查询同样解析该版本，并返回用于 CAS/审计的 `agent_version_id + content_hash`。接口不返回 Agent Draft、Instructions 和私有装配全文。

### 15.4 Group

```text
POST /api/v1/sessions/{id}/groups
GET  /api/v1/groups/{id}
POST /api/v1/groups/{id}/runs
GET  /api/v1/group-runs/{id}
POST /api/v1/group-runs/{id}/messages
POST /api/v1/group-runs/{id}/reporter
POST /api/v1/group-runs/{id}/cancel
GET  /api/v1/groups/{id}/messages
GET  /api/v1/group-runs/{id}/interaction-graph
```

第一阶段建组接受明确 `member_agent_ids[]`，不接受角色模板 ID。响应包含解析后的 AgentVersion 快照、唯一 Run 引用和 Reporter；GroupRun 不返回独立 status/outcome。

### 15.5 等待与验证

```text
POST /api/v1/clarifications/{id}/answer
POST /api/v1/approvals/{id}/approve
POST /api/v1/approvals/{id}/reject
POST /api/v1/runs/{id}/cancel
GET  /api/v1/runs/{id}/verification-results
```

Verification 由系统/UAT 内部规则驱动，不暴露允许客户端伪造结果的写接口，也不要求模型调用。

## 16. SSE 事件

可靠游标、过滤后的 cursor frame、Redis 降级和重连语义以[API 与 SSE](api-sse.md)为准。

可靠事件至少包括：

```text
run.created / run.waiting / run.resumed / run.cancellation_requested / run.completed / run.failed / run.cancelled
agent_turn.started / agent_turn.waiting_actions / agent_turn.completed / agent_turn.failed
message.delta (provisional)
message.created (reliable)
tool_invocation.created / waiting_confirmation / waiting_resource / queued / running / succeeded / failed / rejected / cancelled / outcome_unknown
agent_invocation.created / queued / running / succeeded / failed / rejected / cancelled / timed_out
clarification.requested / clarification.answered / clarification.cancelled
approval.requested / approval.approved / approval.rejected / approval.cancelled
working_plan.updated / plan.permission_entered / plan.permission_exited / plan.implementation_approved
group.created / group.membership_joined / group.membership_ended / group.membership_invitation_rejected
group.message_created / group.summary_requested / group.summary_delivered / shared_task.updated
artifact.published / artifact.unavailable / artifact.restored / artifact.deleted
notification_delivery.created / notification_delivery.succeeded / notification_delivery.failed / notification_delivery.dead / notification_delivery.outcome_unknown
verification.completed
budget.warning
runner.lost / late_result.observed
```

`message.delta` 通过 Redis bridge 低延迟传输，不保证断线后重放；`message.created`、计划、状态、Invocation 和 Artifact 从 MySQL ExecutionEvent 续读。完整稳定集合以[执行事件与可观测性](../modules/observability.md)为准。

## 17. 前端

### 17.1 Chat

- 统一消息流；
- Agent自主规划时显示 WorkingPlan 卡，不强制每个任务显示；
- Clarification、Approval、Tool、Artifact 和 Verification 状态内联；
- 后台 Run 和断线恢复可见。

### 17.2 Plan

- 模式栏明确 `Plan: read-only`；
- 计划、调查消息和风险持续更新；
- 用户可以编辑/反馈、批准、继续规划或取消；
- 批准实施计划只改变 `permission_mode`；后续 Tool 按各自 `never|per_call|session_grant` 策略处理，只有用户已授予匹配范围的 Session Grant 才可复用授权；
- 批准后保持同一 Session，不跳到冻结 DAG 页面；
- 实施过程中 WorkingPlan 可继续更新。

### 17.3 Group

- 从 Agent Network 搜索并直接选择成员；
- 不显示模板/角色槽位/Formation 图编辑器；
- 展示成员卡、版本、状态和当前 WorkingPlan；
- 用户可以 @任一成员；
- 展示 direct/group Message、AgentInvocation 和真实 Interaction Graph；
- SharedTask 是可选协作板；
- 图外邀请显示拒绝或待批准原因；
- Artifact、测试、Verification 和不同意见可见。

## 18. 可观测性

### 18.1 业务事实

MySQL/ExecutionEvent 是以下问题的权威答案：

- 哪个 AgentVersion 和 permission mode 在工作；
- 模型实际看到了哪些 Tool；
- 产生了哪些 Turn、Tool Call 和最终消息；
- 为什么等待、恢复、拒绝或结束；
- WorkingPlan 如何变化；
- Group 成员来自哪里，谁实际联系了谁；
- 哪些测试和 Artifact 真实存在；
- Verification 为什么通过或失败。

### 18.2 Trace

OTel Span 至少覆盖：

```text
runner
context.assemble
provider.attempt
agent.turn
tool.invocation / tool.attempt
agent.invocation + span links
approval.wait
plan.update
group.message
verification
```

Trace 属性使用 ID、状态、耗时、Token、Tool key 和 reason code；不记录凭证、完整 Prompt、Memory、业务文件全文和 Chain-of-Thought。

### 18.3 关键指标

- Provider 首次成功率 / 最终成功率 / repair rate；
- first payload、Turn duration、Run active time；
- Tool/Agent Invocation success、reject、unknown；
- Approval wait 和恢复延迟；
- WorkingPlan update 频率；
- Run waiting age、cancel latency；
- Group message delivery、Agent fan-out、循环；
- exact verification pass rate；
- duplicate side-effect incident；
- lost Runner / fencing reject / late result。

## 19. 安全

- 所有 Agent/Tool 行动传播 Run 创建时固定的 `tenant_id + account_id`；Agent API 来源同时记录 api_key_id 和 `asserted_user_id` 审计快照，不把裸断言表述为已验证用户；
- AgentCard 只暴露发现字段；
- Group Bootstrap 和消息按 visibility 投影；
- 成员不能替用户批准 Tool 或 Plan；
- 一个 Agent 的文字授权不能成为另一个 Agent 的权限事实；
- Plan `plan_readonly` 在 Dispatcher 再次校验；
- 本地 Workspace 强制区分 `agents/<agent_id>` 私有根、`shared` 根和平台只写 Artifact 快照根；共享物理目录不产生跨 Agent 文件权限；
- `publish_artifact` 复制字节到不可变 storage key，源文件后续修改不影响已发布 Artifact；
- Graph 外邀请和 Tool 拒绝必须持久审计；
- Secret、Prompt、Memory 和 Chain-of-Thought 不进入 Message/Trace；
- Local Sandbox 不宣传为恶意代码安全边界。

## 20. 迁移实施阶段

### Stage 0：冻结现场和特征测试

- 保留现有 UAT 数据、镜像、备份和失败 Run；
- 为当前 `finalize_response`、Tool 消费、Plan DAG、Formation JSON 写反向特征测试；
- 固定现有可靠的 Outbox、lease/fencing、Workspace、SSE 和 OTel 测试；
- 每个变更在独立数据库 Schema 上运行，不改写历史 UAT。

### Stage 1：删除 Completion 补丁链

主要代码目标：

- `backend/app/workers/runtime_worker_v1.py`；
- `backend/app/core/model_hub/response_contracts.py`；
- Runner 诊断/API 中的 completion contract 字段；
- Mock Provider 和相关测试。

完成条件：

- 仓库中没有可执行的 `finalize_response`；
- 没有 `required_tool_keys` 业务逻辑；
- 普通最终消息可完成根/子 WorkItem；
- 已使用 Tool 后仍在后续 Snapshot 中可见；
- 已发生副作用在协议失败后不被重放。

### Stage 2：统一 Turn 与 Provider

- 增加 AgentTurn / ProviderAttempt；
- Adapter 统一 Tool Call、Tool Result、Message 和 stop reason；
- 运行 Provider Conformance Suite；
- Context Manifest 关系化；
- 零 payload 重试与 payload 后失败分离。

完成条件：目标 Provider 全部通过最低协议，普通 final 不再依赖结构化输出。

### Stage 3：WorkingPlan

- 增加 WorkingPlan / PlanUpdate；
- 实现 `update_plan` Runtime Tool；
- Chat 可按模型判断显示计划；
- SSE/UI 支持 `working_plan.updated`；
- Context/Compaction 保留当前计划。

完成条件：复杂 Chat 能计划并执行，简单 Chat 不被迫规划；计划更新不触发隐式 Action。

### Stage 4：Plan Permission Mode

- Session 增加 interaction/permission mode 和版本；
- Dispatcher 实现 `plan_readonly`；
- Plan 最终方案进入 Approval；
- 批准后原 Session 恢复；
- 下线冻结 DAG 的新建入口，旧 Plan 数据只读查看。

完成条件：批准前无写入，批准后同 Session 动态执行并可重规划；取消持久化闭环。

### Stage 5：工作网与 Group

- 发布 AgentCard 搜索 API；
- Group 创建直接接受 member Agent IDs；
- Formation Proposal 不再是必经路径；
- Group Membership、RunSnapshot、Message 和 Interaction Graph；
- `fixed_for_run` 拒绝图外邀请；
- 用户可以直接 @成员。

完成条件：产品/开发/测试从工作网选取，无模板完成真实 Group 协作。

### Stage 6：Verification 与 UAT

- 实现 `capacity_risk.v1`；
- 按 ToolAttempt 精确核对九例；
- 校验四个 Artifact；
- 运行三模式功能、恢复、安全和统计样本；
- 归档 clean SHA、模型、AgentVersion、JUnit、Trace 和 Artifact。

### Stage 7：清理与收敛

- 删除不再使用的 Completion/Effect Ledger 代码和 Schema；
- 旧 Plan DAG/Formation API 只读兼容期后移除；
- 更新所有分模块技术文档；
- 清除已废弃 feature flag 和 UI；
- 完成回滚、备份和正式部署演练。

## 21. 测试策略

### 21.1 Unit

- CapabilitySnapshot 与 permission mode；
- Tool 持续可见；
- ToolInvocation 幂等和 unknown outcome；
- Assistant final 的五种 WorkItem purpose 路由；
- Tool 参数一次修复；
- WorkingPlan CAS 和 revision；
- Plan approve subject version；
- Agent Network visibility；
- Group Membership 和 RunSnapshot；
- direct/group Message 路由；
- 图外邀请 rejected record；
- Verification 精确 argv；
- cancel、fencing 和 late result。

### 21.2 Integration

- Provider stream + Tool + final；
- 两次同 Tool 的不同业务调用；
- Tool Confirmation 后恢复；
- Clarification 后恢复；
- AgentInvocation 成功/失败/拒绝；
- Plan 只读→批准→执行；
- Group 三成员 direct message 和反向调用；
- Worker/Sandbox/Collector 重启；
- SSE 断线续读；
- Artifact publish/download；
- 九例 Verification。

### 21.3 反回归

- 用户无需提内部 Tool key；
- 模型文本中的 Tool 名不触发调用；
- 不存在 `finalize_response` Tool Definition；
- 不存在 required effects 合同；
- `execute`、`write_file`、`search_group_history` 可重复选择；
- 普通 final 不因 JSON parse 失败而丢失；
- Plan item 不自动生成 WorkItem；
- Group 创建不调用 Formation Provider；
- 相同 checksum 不自动拒绝合法新 Artifact，也不允许相同 operation key 重复发布；
- 终态 Run 不复活。

## 22. UAT 详细门禁

### 22.1 Chat

自然语言行动清单场景：

- 缺失测试负责人、验收时间和优先级时真实澄清；
- 回答后同 WorkItem 恢复；
- 写入并发布一个 Markdown Artifact；
- 下载内容完整；
- 不出现 execute/agent invocation；
- Runtime Worker 在等待期间重启后无重复写入/发布；
- 重复 10 次，10/10 通过。

### 22.2 Plan Proposal 与批准

- 项目经理进入 `plan_readonly`；
- 可读项目、可澄清、可更新 WorkingPlan；
- 批准前写入/execute/publish 成功数为 0；
- Proposal 30 次均得到可读计划，不要求计划 item 完全相同；
- 用户批准后同 Session 执行；
- 计划适应性样本 5/5：人为注入需求变化或测试失败后产生合理 PlanUpdate；
- 全流程 5/5 真实完成九例和 Artifact；
- 取消样本数据库状态和 UI 一致。

### 22.3 Group 建立

- 产品/开发/测试先存在于 Agent Network；
- 用户直接选择三者；
- Group 创建不调用模型 Formation；
- Snapshot 引用正确 AgentVersion；
- 外部顾问不在 Membership；
- 创建 10 次，10/10 成员和权限正确。

### 22.4 Group 全流程

- 三成员都产生真实 Turn；
- 至少形成产品↔开发、开发↔测试、测试↔产品中的真实双向互动；
- Interaction Graph 来自 Message/AgentInvocation；
- 图外邀请被记录为 rejected；
- `search_group_history` 可以按业务需要多次调用；
- 精确九例 9/9；
- 四个 Artifact 全部存在并可下载；
- 不出现重复 operation key 副作用；
- Sandbox Worker 重启后继续；
- 5 次全流程 5/5 通过。

### 22.5 统计记录

每次记录：

- first-attempt success / final success；
- Provider repair 和 retry；
- first payload / Turn / Run active duration；
- Token、Tool、Agent 调用和成本；
- PlanUpdate 次数；
- waiting/approval 时长；
- verified pass rate；
- rejected、unknown 和 duplicate incident。

必须先通过单次完整功能流，再开始统计批量样本。

## 23. 部署与回滚

### 23.1 UAT 部署

- 单机 Docker Compose；
- 使用独立 `myclaw-uat` Project、端口和 Volume；
- 部署前备份 MySQL 和 Workspace/Artifact；
- 镜像记录 clean SHA 和 build metadata；
- Provider 配置和 AgentVersion 不在测试中热修改；
- Collector 持久目录权限真实烟测。

### 23.2 Feature Flags

迁移期仅使用最少开关：

```text
agent_loop_v2
working_plan_v2
plan_permission_mode_v2
group_network_membership_v2
verification_v1
```

不保留 `finalize_response` 的双写或双跑路径。旧 Session 只读查看，新的 UAT Session 只走 v2。

### 23.3 回滚

回滚只回到部署前镜像和数据库备份，不把新 v2 Run 交给旧 Completion Harness 继续执行。回滚后新数据只读保留并明确标记 incompatible runtime version。

## 24. Definition of Done

第一阶段只有同时满足以下条件才完成：

- 权威产品和技术文档一致；
- 代码中不存在活动的 finalize/required-tool/effect-consumption 主链；
- 三模式共用 AgentLoopCoordinator；
- 普通 final message 在全部 WorkItem purpose 下有测试；
- WorkingPlan 在 Chat/Plan/Group 可用且不驱动业务；
- Plan 是只读权限模式并能原 Session 批准实施；
- Group 成员来自 Agent Network，不依赖模板和 Formation JSON；
- Tool、Agent、Clarification、Approval、取消和恢复闭环；
- 精确九例与四 Artifact 通过 Verification；
- 所有功能、故障注入、统计和可观测性门禁通过；
- UAT 证据绑定 clean SHA、模型、AgentVersion 和环境；
- 旧 UAT 失败事实未被修改；
- 无 P0/P1 未关闭项。

## 25. 实施评审问题

每个 PR 必须回答：

1. 这项改动增强 Agent 的观察和行动，还是替模型决定了业务路径？
2. 是否增加了新的强制对象或结构化协议？为什么现有原语不足？
3. Chat、Plan、Group 是否走同一代码路径？
4. Tool 是否因历史成功而被移除？
5. 是否把 Turn Stop、Run Terminal、Verification 和 UAT 混为一体？
6. Group 成员是否确实来自 Agent Network？
7. 一期限制是否只存在于 Policy/UAT Fixture，而没有写入终局 Schema？
8. Worker 重启、Approval 恢复和迟到结果是否仍安全？
9. 是否能从 ExecutionEvent 和 Snapshot 解释真实行为？
10. 是否需要更新本方案或权威产品模块？
