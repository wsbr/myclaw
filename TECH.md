# MyClaw 技术设计总览

> 本文描述目标技术架构，不表示当前代码完成度。详细契约以 [权威模块文档](docs/README.md) 为准。

第一阶段实现级设计统一从 [技术设计索引](docs/technical/README.md) 进入；本文件只保留架构摘要与导航。

## 架构目标

MyClaw 将稳定 Agent 身份与不可变 AgentVersion 分离。平台注册可复用能力，Runtime 按 Chat 或已确认图的版本规则固定实际装配与 ExecutionPrincipal，通过持久化 Work Item、Invocation、ExecutionEvent 和 Checkpoint 支持等待、重试、取消与多 Agent 协作。

```text
Admin Registries
├── Model Registry
├── Platform Tool Registry
├── Skill Registry + Shared Skill Workspace
├── Sandbox Profile Registry
└── Policy Registries

Execution Runtime
├── Assembly Resolver
├── Context / Prompt Builder
├── Model Adapter
├── Tool Dispatcher
├── Agent Invocation Gateway
├── Durable State Coordinator
├── Session Sandbox Manager
├── ExecutionEvent / Checkpoint
├── ExecutionQueue / Outbox Dispatcher
└── OTel Trace Exporter
```

## 核心实体

| 实体 | 技术职责 |
| --- | --- |
| `Agent` / `AgentVersion` | 保存稳定身份、所有权、访问范围与不可变发布装配 |
| `PlatformTool` | 保存平台代码注册的 Tool 元数据、执行类型、启停和 Confirmation Policy |
| `Skill` / `SkillVersion` | 保存逻辑标识、包位置、版本和 checksum |
| `Model` | 保存 Provider、Endpoint、凭证引用、默认参数和能力 |
| `PolicyDefinition` | 保存四类上下文与记忆策略 |
| `SandboxProfile` / `SessionSandbox` | 保存 Agent 选择和 Session 固定的执行环境 |
| `UserSession` | 保存用户与入口 Agent 的连续对话 |
| `WorkItem` | 保存可跨 Runner 持续的业务工作 |
| `Runner` | 保存一次短生命周期执行及其装配快照 |
| `ToolInvocation` / `ToolAttempt` | 保存语义 Tool 调用与平台物理尝试 |
| `AgentInvocation` | 保存经 `invoke_agent` 发生的定向请求与结果 |
| `Clarification` | 保存可恢复的用户问答等待 |
| `PlanExecution` | 保存已确认并冻结的 Work Item Graph |
| `GroupFormationProposal` | 保存确认前关系化 Group revision、Bootstrap、参与者、初始工作和建议边 |
| `GroupSession` | 保存固定参与者与共享 Message / ExecutionEvent |
| `GroupRun` | 保存 GroupSession 内一次不可重启的工作周期 |
| `GroupAgentNode` | 保存 Agent 在 Group 中唯一的参与实例 |
| `Artifact` | 登记 Session Workspace 中正式发布的文件 |
| `MemoryHead` / `Memory` | 保存用户事实逻辑槽、不可变版本、来源和唯一 active 版本 |
| `PlatformAccount` / `AgentApiKey` | 保存统一 userId、基础平台角色和 Agent 绑定的外部凭证 |
| `ChannelAccount` / `ChannelConversationBinding` | 保存第三方通道与 Session 绑定 |
| `ExecutionEvent` | 保存事务一致、可续读的执行时间线 |
| `PlatformAuditEvent` | 保存不占用 Session sequence 的管理面与安全审计 |
| `NotificationDelivery` | 保存 Channel Session 原来源通知的投递与重试 |
| `RunnerCheckpoint` | 保存 Runner / Work Item 恢复快照 |
| `OutboxCommand` | 第一阶段数据库逻辑多 Topic 执行队列 |

## 模式执行聚合

```text
UserSession
├── Chat Execution
├── PlanExecution
│   └── Frozen Work Item Graph
└── GroupSession
    ├── Fixed Agent Participant Graph
    ├── Shared Messages / ExecutionEvents / Transcript
    └── GroupRun[]
        └── WorkItem[] / Runner[]
```

Plan 图节点是 Work Item，确认后完全冻结；Group 图节点是 Agent 参与实例，确认后参与者固定，但图内协作边和节点 Work Item 可以追加。

## 可恢复 Runtime 数据流

1. Chat 新 Run 解析最新发布 AgentVersion；Plan / Group 使用图确认时固定的 AgentVersion。
2. 固定原始 ExecutionPrincipal、模式范围和调用链。
3. 解析唯一 Model、显式 Tool、Skill 最新版本、四类 Policy 和 Agent Catalog。
4. 加载 Work Item、当前输入及 UserSession / Plan / Group 上下文。
5. 固定 Runner 快照并组装 Prompt。
6. 调用模型和显式 Tool。
7. 遇到用户、Agent 或异步 Tool 等待时，持久化等待对象并结束 Runner。
8. 等待条件满足后，在同一 Work Item 下创建新 Runner。
9. 在领域事务中记录状态、ExecutionEvent 和 Outbox。
10. 结算 Work Item 并执行记忆写入策略。

Runner 不长期挂起，也不原地恢复。正在执行的 Runner 固定 AgentVersion 与实际装配；Skill 在每个新 Runner 启动时按逻辑标识解析并固定 checksum；Sandbox Profile 由 Session 创建时固定。

## Prompt 组装

```text
Platform Runtime Rules
Agent name/description/instructions
已装配 Tool Definitions
Skill Catalog
显式绑定 Agent Catalog
Plan Declared Agent Catalog（Plan）
Group Agent Catalog（Group）
Context Policy 结果
History Policy 结果
Memory Recall 结果
当前 Work Item 与执行上下文
当前定向输入
```

Group 节点从统一共享记录构建自己的上下文投影，不建立独立聊天 Session。Context / History Policy 只能处理 `group_id + agent_id` 的直接可见范围；其他节点记录必须通过显式查询 Tool 获取。

## Invocation 语义

所有 Tool 调用都产生 ToolInvocation：

- `inline` 在当前 Runner 内返回；
- `durable_async` 使 Work Item 进入 `waiting_tool`；
- 平台技术重试属于同一 ToolInvocation，使用相同参数和幂等键；
- 有副作用且无法确认结果时进入 `outcome_unknown`；
- 需要轻量确认时进入 `waiting_confirmation`。

所有 `invoke_agent` 都必须产生定向结果。调用方进入 `waiting_agent`；同一 Runner 可并行发起多个调用，全部进入终态后再恢复调用方。

## 身份与授权

ExecutionPrincipal 至少记录租户、唯一 `userId`、认证来源、UserSession、入口 Agent、当前 Agent、Plan/Group 范围和 delegation chain。

目标 Agent 使用自己的模型、Tool、Skill 和策略，但 Agent 调用不能提权。有效权限是原始主体权限、当前 Agent 装配、模式范围与 Tool/业务系统策略的交集。

Agent API Key 绑定一个入口 Agent，可创建多把并分别撤销、轮换。外部系统传入经过自身认证的企业 `userId`；API Key 采用固定最小能力集，不承载业务角色或自由组合 Scope。完整定义见 [身份与 Agent API](docs/modules/identity-api.md)。

第一阶段平台角色为 `user / builder / admin`，Agent 访问范围为 `private / enterprise`。Builder 拥有蓝图但不拥有使用者数据；显式绑定不授予目标 Agent 访问权，组图、图确认和实际 `invoke_agent` 都必须校验原始 `userId`。

## Group 共享记录

GroupSession 共享 Message 与 ExecutionEvent。每条记录保留 sequence、发送者、接收者、类型、Clarification、Work Item、Invocation 和因果引用。

消息定向处理：只有接收者被立即唤醒。当前 Agent 的策略只能处理其直接可见投影；其他节点的共享记录只有显式调用查询 Tool 后才进入当前 Runner。Agent 私有装配、记忆、Prompt 和内部推理不进入共享记录。

终态 GroupRun 不能重新激活；相关追问在同一 GroupSession 创建新的 GroupRun。新 GroupRun 重新解析全部固定节点：可用节点固定最新版，停用节点保留为 unavailable 且不能接收新工作；非 owner 停用不阻塞其他节点，owner 必须是可用固定节点。

## 一致性、恢复与 Trace

- Chat 在新 Run 创建时解析最新发布 AgentVersion，Plan / Group 在图确认时固定参与 AgentVersion；
- Model、Tool 和 Policy 按固定 AgentVersion 解析，仍受平台注册启停治理；
- Skill 逻辑标识指向最新发布版本，当前 Runner 固定 checksum；
- Tool Confirmation Policy 在真正执行前读取当前 `never | always` 配置；策略收紧影响尚未执行的调用，策略放宽不自动批准既有等待；
- Runtime 不进行模型 fallback、Tool 补齐或参数覆盖；
- 取消停止后续推进但不回滚业务副作用；
- Session 执行状态与 ExecutionEvent 在同一事务提交，管理面状态与 PlatformAuditEvent 在同一事务提交；
- RunnerCheckpoint 只负责恢复，Outbox 只负责可靠调度；
- 第一阶段 Worker 直接轮询一张 `outbox_command` 表，并按 Topic 分池；
- Redis 作为实时 Token Bridge 的必需但非权威依赖；桥接失败不影响 Run、最终 Message 或可靠事件；
- 后期可通过同一 ExecutionQueue 接口接入 MQ，Transactional Outbox 仍保留；
- 同一 Work Item 同时最多一个 active Runner，Worker 使用租约、心跳和 fencing token；
- 可并发状态使用乐观版本号，Session Event sequence 在短事务中分配；
- 每个 Runner 创建独立 OTel Trace，异步调用通过 Invocation ID 和 Span Link 关联；
- Trace 允许异步导出，不作为当前状态、SSE 或恢复依据；
- 历史重跑默认使用重跑时最新可用配置，不承诺完全确定性复现。

第一阶段只支持 Checkpoint 恢复和创建引用历史的新 Run。任意 Fork 与确定性 Replay 后置。

## Sandbox 与文件

Agent 选择 `sandbox_profile_id`，Session 创建时固定 SessionSandbox。第一阶段只有本地目录隔离的 `local`；Local Sandbox 不是不可信代码安全边界。

Session Workspace 使用整体读写锁，`execute` 获取独占锁。默认不主动回收，生命周期与 Session 一致。正式文件仍保存在 Workspace，并以轻量 Artifact 元数据登记。

## Channel

ChannelPlugin 将第三方消息标准化并通过 ChannelConversationBinding 持续复用 Session。Channel 用户必须映射到唯一 `userId`。任务通知默认投递回 Session 原 Channel；通知失败不改变 Run 状态。

## 管理面与执行面

管理面负责 Registry、Agent 草稿与发布、Skill 发布、角色和 Agent 访问范围、Tool 启停与 Confirmation Policy、策略选择、Plan Proposal 和 GroupFormationProposal 确认。

执行面负责 Runner、Prompt、模型、Tool、定向 Agent 调用、等待恢复、身份校验、ExecutionEvent、Checkpoint、状态和 Trace。执行面不能自行改变 Agent 装配、Plan 图或 Group 参与者集合。

## 文档导航

- [文档索引与权威性](docs/README.md)
- [设计哲学与产品原则](docs/DESIGN_PHILOSOPHY.md)
- [企业智能体网络最终蓝图](docs/modules/enterprise-agent-network.md)
- [第一阶段交付边界](docs/modules/phase-one-scope.md)
- [Agent Schema 与装配规则](docs/modules/agent-assembly.md)
- [Platform Tool Registry](docs/modules/tool-system.md)
- [Skill 包、同步与版本](docs/modules/skill-system.md)
- [Model Registry](docs/modules/model-system.md)
- [上下文与记忆策略](docs/modules/context-memory.md)
- [身份与 Agent API](docs/modules/identity-api.md)
- [Sandbox、Workspace 与 Artifact](docs/modules/sandbox-artifact.md)
- [Channel 与通知](docs/modules/channel-system.md)
- [Agent 发现与调用](docs/modules/agent-collaboration.md)
- [Plan 数据模型与生命周期](docs/modules/plan-mode.md)
- [Group 数据模型与生命周期](docs/modules/group-mode.md)
- [Runtime、Prompt 与结算](docs/modules/runtime-execution.md)
- [Runtime 调度与数据库并发](docs/modules/runtime-scheduling.md)
- [执行事件、Checkpoint 与可观测性](docs/modules/observability.md)
- [非权威待确认主题](docs/pending/legacy-designs-to-review.md)
- [第一阶段技术设计索引](docs/technical/README.md)
- [物理架构](docs/technical/architecture.md)
- [数据库 Schema](docs/technical/database-schema.md)
- [状态机与一致性](docs/technical/state-machines.md)
- [执行事件、平台审计与可见性](docs/technical/events-audit-visibility.md)
- [Runtime 详细流程](docs/technical/runtime-flows.md)
- [API、SSE 与前端](docs/technical/api-sse.md)
- [代码审计与迁移计划](docs/technical/audit-migration-plan.md)
