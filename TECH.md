# MyClaw 技术设计总览

> 本文描述目标架构，不表示当前代码完成度。产品语义以[核心模块](docs/README.md#core-concepts)为准，数据、状态和接口细节以[技术契约](docs/technical/README.md)为准。

## 架构目标

MyClaw 的核心是一套模型驱动、可以持久运行和安全行动的 Agent Runtime：

```text
Experience
├── Chat view
├── Plan permission mode + WorkingPlan view
└── Group view + Agent network directory

Agent Runtime
├── Unified Agent Loop
├── Context Assembler / Compaction
├── WorkingPlan projection
├── Tool / Agent collaboration
├── Stop / Continuation policy
└── Message / Artifact delivery

Trusted Execution Kernel
├── Identity / Authority / Capability snapshot
├── Agent directory / local resolver
├── Model adapter / Turn protocol
├── Invocation dispatcher / approval
├── Durable state / checkpoint / cancellation
├── Idempotency / lease / fencing / budget
└── Event / audit / trace

Infrastructure
├── MySQL authority
├── Database Outbox / workers
├── Redis provisional token bridge
├── UAT: Local Sandbox / single-node Workspace
├── Production: Kata/microVM / multi-AZ Workspace / object Artifact
└── OTel backend
```

Runtime 让模型决定如何理解、计划、行动和协作；Kernel 只负责机器事实、权限、可靠性和物理边界。

## 核心不变量

1. **只有一个 Agent Loop**：Chat、Plan、Group 不复制执行内核；
2. **模式是策略**：差异来自权限、交互和协作能力；
3. **计划由模型维护**：WorkingPlan 可选、动态、可见，但不驱动业务；
4. **Group 成员来自工作网**：不依赖模板，不复制 Agent；
5. **能力机器裁决**：自然语言、Skill 和历史文本不产生权限；
6. **Tool 类型不被消费**：每次具体 Invocation 独立治理；
7. **普通消息可以自然结束 Turn**：不存在模型可调用的 `finalize_response`；
8. **结束与验证分离**：真实执行事实决定 verified，不由模型完成信封决定；
9. **Evaluator 非必选**：专项评价按场景接入，不进入通用结算主链；
10. **执行可恢复、历史不可改写**：等待后新建 Runner，终态 Run 不复活；
11. **Context 是有限资源**：只投影当前决策所需的高信号事实；
12. **身份、配置、工作与计算分离**：Agent、AgentVersion、WorkItem 和 Runner 不能合并；
13. **平台内本地寻址**：数据库目录和调度器足够，跨控制域才引入网络 Endpoint；
14. **最少抽象优先**：新实体必须证明无法由现有原语解决。

## 核心实体

| 实体 | 技术职责 |
| --- | --- |
| `Agent` | 稳定逻辑身份、所有权和治理边界，不绑定进程或机器 |
| `AgentVersion` | 不可变 ModelVersion、Instructions、Tool 选择/registration hash、精确 SkillVersion/checksum、PolicyVersion、SandboxProfileVersion 和协作配置蓝图；Tool 启停/确认/风险/连接配置是实时治理例外 |
| `AgentCard` | 工作网发现所需的安全能力投影 |
| `UserSession` | 用户与 Agent 或 Group 的持续上下文 |
| `Run` | 一次用户请求或协作工作的唯一生命周期与终态权威 |
| `AgentTurn` / `ProviderAttempt` | 一次模型决定及其安全传输尝试 |
| `WorkItem` | 可跨等待、恢复和多个 Runner 持续的工作状态 |
| `Runner` | 由 Runtime Worker 承载的一段短生命周期执行及其 lease/fencing 边界 |
| `WorkingPlan` / `PlanUpdate` | 任意模式可选的模型工作计划与变更历史 |
| `CapabilitySnapshot` | 本 Runner 可使用和被拒绝的能力、权限与预算快照 |
| `ToolInvocation` / `ToolAttempt` | 一次语义行动与其物理执行尝试 |
| `AgentInvocation` | Agent 间定向工作请求、等待和结果 |
| `Clarification` / `Approval` | 可恢复的人类输入和高影响确认 |
| `GroupSession` / `GroupMembership` | Chat 模型从治理后候选目录按诉求选择、Chat 后形成的 Agent 协作空间和成员事件 |
| `GroupRun` | Group 中一次目标的协作聚合；与 Run 一对一且不复制状态 |
| `Workspace` / `Artifact` | 强制分区的运行文件空间和不可变正式交付快照 |
| `ExecutionEvent` / `RunnerCheckpoint` | 可靠时间线和恢复快照 |
| `OutboxCommand` | 至少一次的后台调度命令 |

`TaskBrief`、共享任务列表、质量评审和验证报告都可以作为可选投影或 Artifact 存在，不强制成为每个 Run 的领域前置条件。

## 统一 Agent Loop

```text
Resolve runtime snapshot
→ Assemble high-signal context
→ Call model with tools
→ Model returns:
   ├── assistant message
   ├── tool calls
   └── provider stop reason
→ Validate and persist requested actions
→ Execute / wait / return results
→ Continue the same Run with a new Turn or Runner
→ Deliver final message when the model naturally stops
```

模型可以通过 Runtime Coordination Tool 更新计划、请求用户或协作；这些调用具有真实平台语义，但没有“完成整个任务”的控制 Tool。

Provider Adapter 只规范：有序 Turn Item、content part、Tool Call/Result 关联、usage、stream event、stop reason、错误和受控的不透明 continuation state。普通最终文本不要求包进复杂 JSON；Tool 参数仍按各 Tool 的小型 schema 校验。

## Mode Policy

Runtime 在同一 Loop 上解析三类策略：

| 策略 | Chat | Plan | Group |
| --- | --- | --- | --- |
| `interaction_policy` | 单 Agent 对话 | 单 Agent + 计划审批 | 多成员可寻址对话 |
| `permission_mode` | 当前授权 | 批准前 `plan_readonly`，批准后恢复执行权限 | 每个成员自身权限 |
| `collaboration_policy` | 可选委派，结果回当前 Agent | 可选研究/委派，结果回当前 Agent | 成员消息、调用和共享协作 |
| `plan_visibility` | 按需显示 | 始终显示 | 成员计划可私有，共享任务可选 |

Policy 不包含业务步骤，也不根据 Plan 节点自动调度 Agent。

## WorkingPlan

WorkingPlan 是当前 Agent 主动维护的高信号状态：

```text
plan_id
session_id / run_id
owner_agent_id
summary
items[]: text + status
explanation?
revision
updated_at
```

模型可以在任何模式调用 `update_plan`。Runtime 保存 revision 和事件，UI 展示当前版本；更新不创造 ToolInvocation、AgentInvocation 或业务授权。

显式 Plan 模式只通过 `permission_mode=plan_readonly` 阻止写入和外部副作用。用户批准精确 WorkingPlan revision 后写入 `plan.implementation_approved` 事件并恢复后续 Runner 执行权限；若 WorkingPlan 之后继续修订，新 revision 不继承旧批准。

## Agent 本地运行、Agent Network 与 Group

AgentVersion 是配置蓝图，不是常驻服务。一次用户请求、AgentInvocation 或需要处理的 Group 消息创建或恢复 WorkItem；调度器让 Runtime Worker 领取推进权，加载目标 AgentVersion、权威上下文和 CapabilitySnapshot，开始一个 Runner。到达持久等待边界或本段执行结束时 Runner 终结，Agent 身份和 WorkItem 状态继续由数据库保存。

```text
AgentInvocation / Message
→ CollaborationService 校验主体、策略和可见性
→ AgentDirectoryService 解析本地 AgentVersion
→ 调度事务创建或恢复 WorkItem，并创建 queued Runner + Outbox
→ Runtime Worker 领取 Runner，加载配置并执行统一 Agent Loop
→ 结果持久化并按 return route 唤醒调用方或写入 Group
```

平台内没有“每个 Agent 一个 IP/容器”的要求，也不允许 Agent 绕过 CollaborationService 直接互调进程。`agent_id` 是逻辑地址，Runner 所在 Worker 只是临时执行位置。

AgentVersion 发布后生成 AgentCard 并进入可见范围内的工作网。正常 Chat 的 root Runner 冻结当前 `group_enabled`、可见、可用、可调用且治理允许的 Agent `id/name/description` 目录，模型按用户诉求调用 `select_group_members` 固定最小必要集合。Chat Run 完成时，同一结算事务写入幂等 Outbox；Runtime Control Worker 复核所选 Agent 的实时治理状态后创建涌现式 GroupRun。旧手选入口仅作兼容。

Group 不使用角色模板。自动涌现创建或复用：

```text
GroupSession
├── initiator user / entry agent
├── GroupMembership[]
├── GroupMessage[]
├── optional SharedTask[]
└── GroupRun[]
```

GroupSession 以 `dynamic_future` 接受后续 Chat 的成员重算；`GroupRunSnapshot` 仍固定本次使用的成员 AgentVersion、共享范围、权限和预算，保证 UAT 可复现。GroupRun 与 Run 一对一，Run 是 lifecycle、等待和终态的唯一事实源，不需要 Charter 模板或 Formation JSON。

Group 成员可以直接发消息、调用其他成员、更新共享任务和发布 Artifact。`initiator_agent_id` 只是未指定接收者时的默认对话锚点，没有 Supervisor 权限。每轮 `reporter_agent_id` 只承担结构空闲后的最终汇总责任；Coordinator 用持久 summary epoch 幂等唤醒它，汇总提交且二次确认没有 pending work 后才完成唯一 Run。

第一阶段 Agent Directory 直接查询 MySQL 中的 AgentCard 投影；`group_enabled` 只是候选条件，还必须过滤版本发布状态、可见范围、Collaboration Policy、环境兼容性和可用性。未来跨控制域时由 Agent Gateway 把同一 AgentInvocation 映射到外部协议，本地领域模型不随传输方式分叉。

## Capability Snapshot

```text
effective_capabilities =
  AgentVersion assembly
  ∩ original principal authority
  ∩ session/run permission mode
  ∩ collaboration boundary
  ∩ platform governance
  ∩ remaining hard budget
```

Snapshot 记录 assembled、effective 和 denied capabilities 及 reason code。Provider 看到的 Tool Definitions 必须与 effective 集合一致。

第一阶段 Web 使用登录账号；Agent API 使用绑定入口 Agent 的 API Key，并接受调用方提供的裸 `userId`。两种入口都先解析为租户内不可变 `account_id`，CapabilitySnapshot 只用 `tenant_id + account_id` 作为领域主体键，另记录 `api_key_id`、actor Agent、credential/account-authorization/permission revision 和脱敏后的外部身份证据。raw userId 模式只用于受控 UAT/内网，不能声称已经验证终端用户；生产必须使用 `enterprise_oidc_v1`。

成功调用 Tool 不会从集合中移除它。只有权限、模式、治理、预算或 AgentVersion 发生机器态变化时，后续 Runner 才重新解析能力。

## Tool、Runtime Coordination 与幂等

### Platform Tool

访问文件、命令、Artifact 或外部系统，例如 `read_file`、`write_file`、`execute` 和 `publish_artifact`。

### Runtime Coordination Tool

改变真实协作状态，例如：

- `update_plan`；
- `ask_user`；
- `discover_agents`；
- `invite_agent` / `invoke_agent`；
- `send_group_message`；
- `update_shared_task`。

它们和普通 Tool 一样有明确参数、结果、权限和审计，但不代表业务完成，也不关闭其他能力。

所有有副作用的调用先创建 Invocation。技术重试必须复用同一 Invocation、参数和 operation key；模型在新事实下再次行动则创建新的 Invocation。幂等判断由 Tool 自己声明 scope，不能根据 Tool key 或文件 checksum 粗暴去重。

## Turn 停止与 Run 生命周期

Provider 最终消息且没有 Tool Call 时，当前 Turn 自然停止。Runtime 根据调用位置路由消息：

- 根 Run：交付用户；
- AgentInvocation：Chat/Plan 通过 Tool continuation 恢复调用方；Group 写定向交付消息并触发新的调用方 Turn；
- Group 成员：写入定向或共享消息；
- Plan 只读阶段：生成或更新方案并等待用户批准。

Run 的机器状态至少包括：

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

普通 final message 可以完成一次根 Run，但不能把仍在运行的 Invocation 伪造成成功。若存在明确 pending action，Run 保持 waiting 或 active；如果用户要求提前汇报，消息可以先交付，后台状态继续显示。

## Continuation 与验证

Runtime 可以在模型准备停止时运行轻量 Continuation Check：

1. 是否仍有本 Run 创建且必须等待的 Invocation；
2. 是否存在未回答 Clarification 或 Approval；
3. 是否触发取消、硬预算或协议错误；
4. 当前动作是否违反确定性发布 Gate。

Check 只读取机器事实。需要继续时，把精确事实作为下一 Turn 的 Observation；不调用通用 Evaluator，不让模型声明 required Tool。

结果验证分层：

- `observed`：存在真实 Tool/Agent 结果；
- `verified`：命中场景定义的确定性检查，例如九例实际 argv、退出码和输出；
- `reviewed`：可选评审 Agent 或人类完成评价；
- `accepted`：外部 UAT 或业务 Owner 接受。

这些标签不改变历史事实。未验证结果可以正常交付，但 UI 必须准确显示其状态。

## Context Assembly

Prompt Package 按当前 Turn 动态组装：

```text
Platform constitution
AgentVersion instructions
Current user or agent input
Effective Tool definitions
Relevant Skill content
WorkingPlan summary
Unconsumed Tool / Agent / human results
Selected history / memory
Group scoped messages or shared tasks when relevant
Budget / permission / environment summary
```

每段保留来源、版本、visibility 和 token estimate。Context Assembler 优先引用高信号事实，旧 Tool 输出可以压缩为摘要和稳定引用。其他 Agent 的 Instructions、私有 Memory、凭证和 Chain-of-Thought 不进入共享上下文。

## 可靠执行

- 领域状态、ExecutionEvent 和 Outbox 同事务提交；
- 同一 WorkItem 最多一个 active Runner；
- Runner 使用 lease、heartbeat、execution epoch 和 fencing；
- waiting 条件满足后，调度事务创建新 queued Runner 与 Outbox；任意 Runtime Worker 都可以领取，不恢复旧进程，也不要求回到原机器；
- ToolInvocation 是副作用恢复边界；
- Provider 在零 payload 的安全条件下才进行透明技术重试；
- 已产生 Tool Call 或部分副作用后只按权威记录恢复；
- 预算在 Run 级跨 Runner 累计；
- Redis、SSE delta 和 OTel 都不是业务事实源；
- 终态 Run 不重新激活，后续请求创建新 Run 并引用历史。

Session Workspace 物理上可共享，但 File Tool 强制区分每个 Agent 的私有根、Group shared 根和平台只写 Artifact 快照根。`publish_artifact` 复制源字节并固定 checksum/storage key；源文件后续修改不会改变已发布 Artifact。

## 可观测性

每次执行应能查询：

- AgentVersion、Model、Tool、Skill、Sandbox 和权限快照；
- Context section manifest 与压缩版本；
- 每个 Turn、ProviderAttempt、Tool Call、Message 和 stop reason；
- WorkingPlan 更新、Agent 发现、邀请、消息和 Invocation；
- Approval、等待、恢复、取消、预算和 fencing；
- Workspace、Artifact、测试和机器验证结果；
- 最终消息与 Run 终止原因。

不持久化模型私有 Chain-of-Thought；只保存供应商允许的 reasoning summary、模型可见计划、Action 和事实。

## 第一阶段部署

第一阶段继续采用单机 Docker Compose：Web、API、runtime worker、tool/sandbox worker、maintenance、MySQL、Redis 和可查询 OTel 后端。该拓扑只用于受控 UAT，不承诺企业生产 HA、SLO、RPO/RTO 或不可信代码隔离；生产门禁见[生产就绪要求](docs/operations/production-readiness.md)。

## 企业生产部署

生产不延续单机拓扑：`enterprise_production_v1` 使用三可用区 Kubernetes、MySQL 单写 HA 与 PITR、`enterprise_oidc_v1`、Kata/microVM Sandbox、多可用区 Workspace、版本化对象 Artifact、Redis HA 和 OpenTelemetry。Agent 仍是逻辑配置，Runner 仍由共享 Worker 按需承载；生产不会退回“每 Agent 一个 IP/服务”的模型。

生产默认 SLO 为 API 99.90%，MySQL RPO ≤5 分钟/RTO ≤60 分钟；其余性能、Workspace/Artifact 恢复目标、网络、安全、发布和演练合同见[企业生产参考架构](docs/technical/production-reference-architecture.md)与[生产就绪要求](docs/operations/production-readiness.md)。接口实现必须符合 [OpenAPI 3.1](docs/technical/openapi-v1.yaml)。这些文档定义落地方式；当前实现是否已满足仍只由绑定 clean SHA 的测试和演练证据决定。

## 文档导航

- [设计哲学](docs/DESIGN_PHILOSOPHY.md)
- [产品总览](PRODUCT.md)
- [Enterprise Agent Network](docs/modules/enterprise-agent-network.md)
- [Agent 装配](docs/modules/agent-assembly.md)
- [Agent 协作](docs/modules/agent-collaboration.md)
- [Plan 模式](docs/modules/plan-mode.md)
- [Group 模式](docs/modules/group-mode.md)
- [Runtime 执行](docs/modules/runtime-execution.md)
- [验证说明](docs/VALIDATION.md)
- [数据库契约](docs/technical/database-schema.md)
- [状态机](docs/technical/state-machines.md)
- [API 与 SSE](docs/technical/api-sse.md)
- [OpenAPI 3.1](docs/technical/openapi-v1.yaml)
- [企业生产参考架构](docs/technical/production-reference-architecture.md)
- [生产就绪要求](docs/operations/production-readiness.md)
- [企业生产运行手册](docs/operations/production-runbook.md)
