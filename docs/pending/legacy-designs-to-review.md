# 待确认的旧设计

> **非当前设计、不可作为实现依据。**
>
> 本文只保存历史文档中尚未重新讨论的设想。任何条目必须经过产品与架构决策，并满足 [设计哲学与产品原则](../DESIGN_PHILOSOPHY.md)，才能进入权威模块。

## 使用方式

每项记录四部分：旧设想摘要、原始目标、待讨论问题、与当前原则的潜在冲突。这里的内容不表示路线图、兼容承诺或当前实现要求。

## Task / Celery 长任务模型

- **旧设想摘要**：所有长执行抽象为 Task，并以 Celery 队列调度。
- **原始目标**：支持异步、重试、取消和后台执行。
- **已确定边界**：产品领域使用 Session、Run、WorkItem、Runner、AgentTurn、ToolInvocation、WorkingPlan 和 GroupRun；第一阶段一张 Database Outbox 表按 Topic 直接供 Worker 轮询；同一 WorkItem 只有一个 active Runner，并使用租约、fencing token 和乐观版本控制。
- **后期设计**：保留统一 ExecutionQueue 接口，通过 Outbox Relay 接入 MQ；Transactional Outbox 继续存在。
- **待讨论问题**：MQ 选型、分区、消费组、死信队列、积压治理、租户权重和复杂调度公平性。
- **潜在冲突**：不能把 Celery 等基础设施 Task 重新暴露成产品节点。

## MCP

- **旧设想摘要**：维护 MCP Server Registry，并允许 Agent 选择或通过 Skill 网关调用 MCP 工具。
- **原始目标**：接入外部垂类服务生态。
- **待讨论问题**：MCP 是独立装配维度还是转化为 Platform Tool；凭证、发现、授权和租户隔离；与 Skill 指导调用的关系。
- **潜在冲突**：Skill 不能自动授予 MCP 权限，MCP 也不能形成第二个模糊 Tool 体系。

## Fork 与精确重跑

- **旧设想摘要**：从历史会话或 Span 分叉，使用历史配置精确重现并继续执行。
- **原始目标**：调试、审计和方案比较。
- **已确定边界**：第一阶段只支持 Checkpoint 恢复、同一 Work Item 重试和创建引用历史的新 Run；终态 Run 不复活。
- **后期问题**：只读模拟 Replay、历史环境保留、模型与外部 Tool 非确定性、副作用隔离和 Fork 后记忆作用域。
- **潜在冲突**：不能把 Observability Trace 当作可执行状态，也不能重放已产生副作用的 ToolInvocation。

## Sandbox 安全治理

- **旧设想摘要**：对 Skill 脚本和 `execute` 提供文件、网络、进程及资源隔离。
- **原始目标**：安全运行第三方 Skill。
- **已确定边界**：第一阶段 Agent 选择 `local` Sandbox；Session 固定目录隔离 Workspace；Skill 脚本默认可信；Local 不是不可信代码安全边界。
- **待讨论问题**：`remote` / `aio` 实现、Skill 上传风险评估、信任级别、网络与系统调用策略、资源配额和跨 Sandbox Workspace 挂载。
- **潜在冲突**：不能通过禁止通用 Skill 包结构来替代真正的运行时隔离。

## 统计、预算与告警

- **旧设想摘要**：按 Agent、用户、模型和 Tool 统计成本、Token、延迟并配置预算告警。
- **原始目标**：运营治理和成本控制。
- **已确定边界**：第一阶段 Run 预算跨 Runner 累计 Turn、Attempt、Tool、Agent、Token、active time、fan-out 和协议修复；软阈值把剩余预算反馈 Agent，硬阈值停止新 Action 并明确结束或汇报。
- **待讨论问题**：租户级配额、部门分摊、Group成员成本归属、动态计价、公平调度和告警通道。
- **潜在冲突**：预算策略不应静默切换模型或删减 Agent 已装配能力。

## 高级 API Key Scope

- **当前设计**：第一阶段 Agent API Key 绑定入口 Agent，可创建多把并独立轮换、撤销；调用方通过 `X-MyClaw-User-Id` 断言任意 userId。该模式只用于受控 UAT/内网，生产前升级为受限委托身份或 OIDC/OBO。
- **后期目标**：出现明确集成隔离需求后再支持细粒度 Scope。
- **待讨论问题**：Scope 粒度、兼容策略、账号预创建权限和跨 Agent 管理接口。
- **潜在冲突**：Scope 不能成为绕过 Agent 装配或业务授权的第二套权限系统。

## Tool 风险判断与高级审批

- **当前目标**：第一阶段由 Platform Tool Registry 配置 `never | per_call | session_grant`；Grant严格绑定用户、Session、Tool、Sandbox、参数约束、风险版本和到期时间，Agent不得覆盖。
- **原始目标**：在风险操作前保持用户控制，同时不绑定平台前端。
- **待讨论问题**：业务语义级动态风险判断、租户差异、跨Session预授权、外部业务审批和复杂 Governance Profile。
- **潜在冲突**：不能把所有垂类业务审批写死在平台，也不能允许普通请求自行跳过已生效的确认规则。

## 高级平台权限与 Agent 访问

- **当前能力**：第一阶段只有 `user / builder / admin`，Agent 只有 `private / enterprise`；Builder 拥有蓝图但不拥有用户数据，Agent 调用继承 Run 创建时固定的 canonical userId；API 来源另审计 api_key_id 与 asserted userId。
- **后期目标**：在明确企业治理需求下支持部门、用户组、白名单、审计员和运维角色。
- **待讨论问题**：组织树同步、组合角色、资源级授权、紧急审计访问、审批与数据脱敏。
- **潜在冲突**：高级权限不能让 Agent 绑定绕过用户访问权，也不能让 Builder 默认读取使用者会话。

## K8s 与生产部署

- **旧设想摘要**：提供 Docker Compose 到 Kubernetes 的生产部署方案。
- **原始目标**：高可用、弹性伸缩和企业部署。
- **待讨论问题**：共享 Skill 工作空间的一致性、Runner 调度、状态存储、对象存储和多租户隔离。
- **潜在冲突**：基础设施选择不应被写成产品领域实体，也不能破坏 Skill 版本固定语义。

## Agent 进化治理

- **当前边界**：只预留 `agent` 跨用户记忆作用域；当前不启用自动进化。
- **后期目标**：从 Session / Runner 事实中总结过程经验，生成 Skill Candidate，经管理员审核后发布。
- **待讨论问题**：隐私去标识、样本门槛、质量评估、回滚、候选合并和管理员工作流。
- **潜在冲突**：不能自动修改 Agent Instructions、装配项或已发布 Skill。

## Capability / Event Manifest

- **旧设想摘要**：在 Agent 配置中手写能力和事件 Manifest，用于发现、路由和订阅。
- **原始目标**：结构化表达 Agent 能力与事件协作。
- **待讨论问题**：是否仍有 Tool/Skill/Agent description 无法表达的需求；若有，应由平台派生还是新增受控 Registry。
- **当前替代设计**：发布时从 AgentVersion 生成并由 Builder 确认版本化 AgentCard；能力和协作摘要来自身份、Instructions、真实装配与可信运行事实，不允许手写权限 Manifest。
- **潜在冲突**：AgentCard不能泄露Instructions、Tool细节、Memory、Prompt或私有思维，也不能成为调用授权。

## 旧 Group DAG 与中心编排

- **旧设想摘要**：中心 Orchestrator 生成静态任务 DAG，由 Supervisor 推进 Group 节点。
- **原始目标**：集中控制多 Agent 顺序、依赖与总体完成状态。
- **当前替代设计**：单 Agent 长任务由同一 Session 中的动态 WorkingPlan 辅助；显式 Plan 只是只读权限模式。Group 从 Agent Network 选择真实 Agent，以 Membership、Message、可选 SharedTask 和实际 Interaction Graph 形成对等社会。
- **潜在冲突**：Group 成员是完整 Agent 而非一次性 Task，没有固定 Supervisor；WorkingPlan 和 SharedTask 都不能被平台编译成业务 DAG。
