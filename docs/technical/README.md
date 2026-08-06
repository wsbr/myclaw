# 第一阶段技术设计索引

## 当前权威基线

[第一阶段 Agent-native 交付技术方案](phase-one-agent-native-delivery.md)是第一阶段架构、Provider 协议、迁移、测试和 UAT 门禁的总纲。详细实施权威拆分为[数据库契约](database-schema.md)、[状态机](state-machines.md)、[API 与 SSE](api-sse.md)和机器可读 [OpenAPI 3.1](openapi-v1.yaml)。[企业生产参考架构](production-reference-architecture.md)把同一领域语义映射为 HA、OIDC/OBO、强 Sandbox、分布式存储、发布和灾备合同。

[Agent-native Harness 调研与架构判断](agent-native-harness-research.md)记录 Codex、Claude Code、Agent Harness 研究和本项目 UAT 证据，用于解释为什么采用统一 Agent Loop、Plan Permission Mode、工作网 Membership 和自然 Turn Stop。

产品语义仍以[设计文档索引](../README.md)中的权威模块为准。若综合技术方案与产品模块冲突，必须先修订并明确决策，不能由实现自行选择。

## 架构基线

```text
Agent = stable identity + governance boundary
AgentVersion = Model + Instructions + Tools + exact SkillVersions + Policies + Environment
WorkItem = durable work state
Runner = ephemeral execution segment on a Runtime Worker

Agent Loop = Observe → Decide → Act → Observe → Adapt → Respond/Wait

Chat  = Loop + direct execution policy
Plan  = Loop + plan_readonly / approval policy
Group = Loop + Agent Network membership / messaging policy
```

同一 MyClaw 控制域内，Agent Directory 由数据库 AgentCard 投影提供，AgentInvocation 通过 WorkItem、Outbox 和 Runtime Worker 按需运行目标配置；不存在每个 Agent 一个常驻服务。跨控制域的 Endpoint 和协议只进入未来 Agent Gateway。

第一阶段明确不再使用：

- 模型可调用 `finalize_response`；
- 模型声明 `required_tool_keys`；
- Tool 类型在一次成功后被移除；
- 普通最终消息的复杂结构化 Completion Envelope；
- Plan 确认后冻结 DAG；
- Plan 固定参与者集合；
- Group 模板、角色槽位和强制 Formation JSON；
- 永久 Supervisor；
- 通用 `GoalEnvelope → OutcomeClaim → Evaluator` 结算链；
- 从 Tool 类型或调用次数推断业务成功。

## 文档分层

| 层级 | 文档 | 地位 |
| --- | --- | --- |
| 产品最高原则 | [设计哲学](../DESIGN_PHILOSOPHY.md) | 所有设计的最高约束 |
| 产品模块 | `../modules/*.md` | 相应产品语义权威 |
| 目标技术总览 | [根技术设计总览](../../TECH.md) | 架构摘要和导航 |
| 实施总纲 | [第一阶段交付技术方案](phase-one-agent-native-delivery.md) | 架构、迁移、测试与 UAT 总纲 |
| 数据权威 | [数据库契约](database-schema.md) | 表、字段、约束和索引 |
| 状态权威 | [状态机](state-machines.md) | 迁移、推进者、终态和恢复 |
| 接口语义 | [API 与 SSE](api-sse.md) | 认证、资源、错误、幂等和事件续读 |
| 接口机器契约 | [OpenAPI 3.1](openapi-v1.yaml) | 路径、方法、Header、Schema 和枚举 |
| 生产部署权威 | [企业生产参考架构](production-reference-architecture.md) | HA、身份、Sandbox、存储、扩缩、发布和故障语义 |
| 生产准入 | [生产就绪要求](../operations/production-readiness.md) | SLO、RPO/RTO、安全、演练与证据 Gate |
| 生产运维 | [企业生产运行手册](../operations/production-runbook.md) | 发布、回滚、failover、DR、安全事件与演练步骤 |
| 设计依据 | [Harness 调研](agent-native-harness-research.md) | 外部研究、UAT事实和取舍 |
| 验收证据 | `../acceptance/*` | 历史实现与 UAT 事实，不定义当前架构 |

## 实施契约覆盖矩阵

| 领域 | 产品语义 | 数据/状态 | 外部合同 | 生产实现/验证 |
| --- | --- | --- | --- | --- |
| 身份与账号 | [Identity](../modules/identity-api.md) | account/external identity/composite FK | auth/account paths + identity errors | OIDC/OBO、预配、break-glass、负向测试 |
| Agent/Registry | [Assembly](../modules/agent-assembly.md)、[Model](../modules/model-system.md)、[Skill](../modules/skill-system.md) | logical definition + immutable versions + publish transaction | Agent/Draft/Version/Option schemas | Registry reconcile、Conformance、供应链 Gate |
| Chat/Plan/Group | [Plan](../modules/plan-mode.md)、[Group](../modules/group-mode.md) | sole Run lifecycle + WorkingPlan + Group snapshot/summary epoch | Session/Run/Plan/Group paths | 多副本 Worker、恢复、自然结束与并发测试 |
| Agent Network | [Network](../modules/enterprise-agent-network.md)、[Collaboration](../modules/agent-collaboration.md) | AgentCard/Membership/AgentInvocation | discovery/invocation schemas | 本地 Directory；联邦不属于 v1 生产范围 |
| Tool/Approval | [Tool](../modules/tool-system.md) | ToolInvocation/Attempt/SessionGrant/Approval | RequiredAction/Approval errors | operation key、outcome_unknown、实时治理 |
| Workspace/Artifact | [Sandbox](../modules/sandbox-artifact.md) | SessionSandbox/RunnerSandbox/lock/Artifact | list/download/checksum | Kata、跨区 Workspace、对象不可变与对账 |
| Context/Memory | [Context/Memory](../modules/context-memory.md) | PolicyVersion/MemoryVersion/HistorySummary | 用户设置/管理投影按模块实现 | account scope、visibility、Retention |
| Channel（可选） | [Channel](../modules/channel-system.md) | verified binding/DeliveryAttempt | Provider adapter 内部入口 | 插件签名、重放、原渠道恢复 |
| 调度/事件 | [Scheduling](../modules/runtime-scheduling.md)、[Observability](../modules/observability.md) | Outbox/lease/fencing/Event sequence | REST/SSE cursor | HA Worker、Redis 降级、SLI/告警 |
| 生产运行 | — | release manifest/evidence references | readiness metadata | [参考架构](production-reference-architecture.md)、[准入](../operations/production-readiness.md)、[Runbook](../operations/production-runbook.md) |

矩阵中的每一行都必须有实现 Owner 和测试证据；若新增产品能力无法落入某行，先补产品/数据/状态/API/生产五个维度再编码。`public federation`、通用 MCP、动态组织审批和自动 Agent 进化不属于 `enterprise_production_v1`，不得因本矩阵未展开而被误认为当前缺口。

## 历史资料边界

已被 Agent-native v2 取代的 V1 架构、流程、前端、补丁方案和迁移草案不保留在活动文档树中。仍然有效的 MySQL、Outbox、lease/fencing、Tool 副作用、Workspace/Artifact、SSE、OTel、CAS 等机制已经写入当前权威契约；实现不得从 Git 历史恢复旧 Completion Contract、Effect Ledger、冻结 Plan DAG、Group Formation、固定 Group 图或 Tool 类型消费语义。

`../acceptance/` 中的报告与原始证据继续保留，用于说明历史实现和测试事实，但不能作为当前架构或完成状态的依据。需要考古旧方案时使用 Git 历史，不在活动导航中建立第二套设计来源。

## 部署 Profile

### `uat_single_node`

- MySQL 8、SQLAlchemy 2 Async、Alembic，隔离级别基线 `READ COMMITTED`；
- 应用生成 UUIDv7，公开 ID `CHAR(36)`，UTC `DATETIME(6)`；
- Database Outbox 是可靠调度入口；
- Redis 只做非权威 Token bridge 和短 TTL 设施；
- Session Workspace、Skill 和 Artifact 本体使用受控文件系统；
- SSE 从已提交 ExecutionEvent 续读，模型 delta 不可靠重放；
- 同一 WorkItem 最多一个 active Runner，使用 lease 和 fencing；
- OTel Trace 不作为业务状态或恢复事实；
- 第一阶段没有生产数据迁移负担，但旧 UAT 数据必须只读保留。

### `enterprise_production_v1`

- 三可用区 Kubernetes，无状态服务多副本；
- MySQL 8 单写者 + 跨区 failover replica + PITR；
- `identity_profile=enterprise_oidc_v1`，内部统一 account FK；
- Kata/microVM Sandbox 专用节点池、多可用区 Workspace、版本化对象 Artifact；
- Database Outbox 继续是可靠命令源，Redis/MQ/Trace 均非业务权威；
- 具体副本、网络、发布、故障处理和数值目标见[生产参考架构](production-reference-architecture.md)与[生产就绪要求](../operations/production-readiness.md)。

## 维护规则

- 目标变化先更新产品模块，再更新综合技术方案；
- 新抽象必须证明无法由现有 Agent Loop 原语解决；
- 历史验收材料与当前契约冲突时，以产品模块和当前综合方案为准；
- UAT 报告记录事实，不反向定义产品；
- 完成度只来自 clean SHA、部署、测试和可访问证据；
- 每个实施 PR 必须检查是否重新引入 Workflow、模板或通用完成合同；
- 未提交工作区不称为 release baseline；只有 clean SHA + tag/reference + 绑定证据才可进入 UAT/生产评审。
