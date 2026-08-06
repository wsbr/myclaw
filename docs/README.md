# MyClaw 设计文档索引

## 文档权威性

以下文档描述 MyClaw 的目标设计，是产品与技术实现的依据：

1. [设计哲学与产品原则](DESIGN_PHILOSOPHY.md)：所有模块必须遵守的最高约束。
2. [产品设计总览](../PRODUCT.md)：产品定位、体验和能力地图。
3. [技术设计总览](../TECH.md)：目标架构、实体和 Runtime 数据流。
4. `modules/` 下的模块文档：相应术语和规则的唯一完整定义。
5. [第一阶段 Agent-native 交付技术方案](technical/phase-one-agent-native-delivery.md)：第一阶段架构、实现路线和验收总纲；只展开模块规则，不重新定义产品语义。
6. [数据库契约](technical/database-schema.md)、[状态机](technical/state-machines.md)、[API 与 SSE](technical/api-sse.md)和 [OpenAPI 3.1](technical/openapi-v1.yaml)：字段、约束、状态与 wire contract 的详细技术权威。
7. [企业生产参考架构](technical/production-reference-architecture.md)与[生产就绪要求](operations/production-readiness.md)：生产拓扑、安全、SLO、RPO/RTO、发布和证据权威。
8. [Agent-native Harness 调研与架构判断](technical/agent-native-harness-research.md)：外部产品机制、UAT 证据与设计取舍依据，不覆盖产品权威。
9. `technical/` 下仍标记“历史 V1 实现参考”的文档：只用于迁移时复用可靠工程机制，不属于当前目标设计。

[待确认旧设计](pending/legacy-designs-to-review.md) 不是当前设计，不能作为实现依据。它只保存尚未重新讨论的历史设想。

若摘要与模块文档冲突，以模块文档为准；若模块规则与设计原则冲突，应先修订设计并显式记录决策，不能在实现中自行绕过原则。

技术设计若与产品模块冲突，以产品模块为准并暂停实现确认。当前技术权威迁移关系、V1 可复用边界和冲突处置见[第一阶段技术设计索引](technical/README.md)。

## 权威模块

| 模块 | 唯一负责定义 |
| --- | --- |
| [Enterprise Agent Network](modules/enterprise-agent-network.md) | 平台内工作网、长期联邦方向与第一阶段关系 |
| [第一阶段交付边界](modules/phase-one-scope.md) | 第一阶段必须闭环的产品范围、入口和验收场景 |
| [Agent 装配](modules/agent-assembly.md) | Agent身份、Instructions、AgentCard、版本和装配约束 |
| [Platform Tool](modules/tool-system.md) | Tool 注册、治理和有效集合 |
| [Skill](modules/skill-system.md) | 包格式、共享同步、加载与版本 |
| [Model](modules/model-system.md) | 模型注册、选择与失败语义 |
| [身份与 Agent API](modules/identity-api.md) | 内部 account、一期 raw userId 风险、生产 OIDC/OBO、平台角色与 Agent API |
| [Sandbox 与 Artifact](modules/sandbox-artifact.md) | Sandbox 装配、Workspace、锁、回收和正式文件 |
| [Channel 与通知](modules/channel-system.md) | 第三方消息接入、Session 绑定与来源通知 |
| [上下文与记忆](modules/context-memory.md) | 四类策略和记忆作用域 |
| [Agent 协作](modules/agent-collaboration.md) | AgentCard发现、委派、邀请、消息、授权与 AgentInvocation |
| [Plan 模式](modules/plan-mode.md) | 只读权限模式、WorkingPlan、用户批准和原Session实施 |
| [Group 模式](modules/group-mode.md) | 工作网选人、Membership、消息、对等协作和动态成员演进 |
| [Runtime 执行](modules/runtime-execution.md) | 统一Agent Loop、Turn、能力、Action、停止、验证和恢复 |
| [Runtime 调度与数据库并发](modules/runtime-scheduling.md) | ExecutionQueue、Outbox、Worker 租约和推进权 |
| [执行事件与可观测性](modules/observability.md) | ExecutionEvent、Checkpoint 与 OTel Trace |
| [产品价值验证](modules/product-validation.md) | 用户、替代方案、价值指标、成本与多 Agent 对照实验 |

## 当前技术设计

- [第一阶段 Agent-native 交付技术方案（当前权威）](technical/phase-one-agent-native-delivery.md)
- [数据库契约](technical/database-schema.md)
- [状态机与推进契约](technical/state-machines.md)
- [API 与 SSE 语义契约](technical/api-sse.md)
- [OpenAPI 3.1 机器契约](technical/openapi-v1.yaml)
- [企业生产参考架构](technical/production-reference-architecture.md)
- [Agent-native Harness 调研与架构判断](technical/agent-native-harness-research.md)
- [技术设计索引与已确定选型](technical/README.md)
- [第一阶段 UAT 运行手册](operations/phase-one-runbook.md)
- [企业生产就绪要求](operations/production-readiness.md)
- [企业生产运行手册](operations/production-runbook.md)

## 历史实现与迁移参考

以下文档保留 V1 的物理结构、接口和工程证据，其中的冻结 Plan DAG、Group Formation、Completion Contract 等语义已经失效。新实现只能复用其被当前综合技术方案明确确认的机制：

- [V1 物理架构](technical/architecture.md)
- [V1 事务、一致性与并发](technical/consistency-concurrency.md)
- [V1 事件、审计与可见性](technical/events-audit-visibility.md)
- [V1 Runtime 流程](technical/runtime-flows.md)
- [V1 Context、History 与 Memory](technical/context-history-memory.md)
- [V1 Tool 与 Skill 接口](technical/tools-skills.md)
- [V1 前端设计](technical/frontend.md)
- [旧三模式 Agent Harness 方案](technical/agent-harness-stability-plan.md)
- [历史代码审计与迁移计划](technical/audit-migration-plan.md)
- [历史交付状态](technical/phase-one-delivery-status.md)

## 验收报告

- [首次测试基线：一期真实业务 UAT 报告（2026-08-03）](acceptance/phase-one-uat-2026-08-03.md)
- [修复后持久 UAT 复验报告（2026-08-03）](acceptance/phase-one-uat-revalidation-2026-08-03.md)
- [Harness 修复实现与候选验收报告（2026-08-03）](acceptance/phase-one-harness-remediation-2026-08-03.md)
- [小型软件项目开发组三模式UAT报告（2026-08-04）](acceptance/phase-one-dev-team-uat-2026-08-04.md)
- [验收文档与证据索引](acceptance/README.md)

## 维护规则

- 一个术语只在其所属模块完整定义；其他文档只做摘要并链接。
- 权威文档只描述目标设计，不记录代码完成度。
- 尚未达成决策的主题进入 `pending/`，不得用模糊措辞写成正式承诺。
- 新设计先验证是否满足 [设计哲学](DESIGN_PHILOSOPHY.md)，再进入所属模块。
- 文档和代码的完成度评估应另行形成审计报告，不反向污染目标设计。
- 现有代码差距和处置只写入技术审计文档，不写入产品权威模块。
- 历史文档不得使用“当前基线”“当前契约”等措辞；引用历史机制时必须链接回当前综合技术方案中的对应决策。

## 可追溯设计基线

工作区中的未提交文件只是编辑中的设计，不能称为发布基线。一个可追溯基线必须同时具备：

1. clean Git commit SHA；
2. release tag 或受保护的 release reference；
3. 同一提交中的产品模块、数据库、状态机、API 和决策变更；
4. 文档链接/术语检查与评审记录；
5. UAT 或生产证据明确绑定该 SHA、模型、AgentVersion 和环境。

当前文档即使语义已收敛，只要尚未提交就仍是 working design。是否提交、打 tag 和发布由项目 Owner 决定，文档不能替代 Git 事实。
