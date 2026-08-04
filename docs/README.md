# MyClaw 设计文档索引

## 文档权威性

以下文档描述 MyClaw 的目标设计，是产品与技术实现的依据：

1. [设计哲学与产品原则](DESIGN_PHILOSOPHY.md)：所有模块必须遵守的最高约束。
2. [产品设计总览](../PRODUCT.md)：产品定位、体验和能力地图。
3. [技术设计总览](../TECH.md)：目标架构、实体和 Runtime 数据流。
4. `modules/` 下的模块文档：相应术语和规则的唯一完整定义。
5. `technical/` 下的技术模块：在不改变上述产品语义的前提下，分别作为物理架构、Schema、状态机、Runtime 接口和实施方案的唯一实现级定义。

[待确认旧设计](pending/legacy-designs-to-review.md) 不是当前设计，不能作为实现依据。它只保存尚未重新讨论的历史设想。

若摘要与模块文档冲突，以模块文档为准；若模块规则与设计原则冲突，应先修订设计并显式记录决策，不能在实现中自行绕过原则。

技术模块若与产品模块冲突，以产品模块为准并暂停实现确认；技术模块之间不得复制完整定义，职责与导航见 [第一阶段技术设计索引](technical/README.md)。

## 权威模块

| 模块 | 唯一负责定义 |
| --- | --- |
| [企业智能体网络最终蓝图](modules/enterprise-agent-network.md) | 长期产品北极星、企业工作形态与第一阶段关系 |
| [第一阶段交付边界](modules/phase-one-scope.md) | 第一阶段必须闭环的产品范围、入口和验收场景 |
| [Agent 装配](modules/agent-assembly.md) | Agent Schema、配置页和装配约束 |
| [Platform Tool](modules/tool-system.md) | Tool 注册、治理和有效集合 |
| [Skill](modules/skill-system.md) | 包格式、共享同步、加载与版本 |
| [Model](modules/model-system.md) | 模型注册、选择与失败语义 |
| [身份与 Agent API](modules/identity-api.md) | 统一 userId、平台角色、Agent 访问、API Key 与异步 API |
| [Sandbox 与 Artifact](modules/sandbox-artifact.md) | Sandbox 装配、Workspace、锁、回收和正式文件 |
| [Channel 与通知](modules/channel-system.md) | 第三方消息接入、Session 绑定与来源通知 |
| [上下文与记忆](modules/context-memory.md) | 四类策略和记忆作用域 |
| [Agent 协作](modules/agent-collaboration.md) | 显式绑定、发现与 `invoke_agent` |
| [Plan 模式](modules/plan-mode.md) | 冻结的 Work Item Graph 与严格星形协作 |
| [Group 模式](modules/group-mode.md) | 临时协作群、图、定向澄清和生命周期 |
| [Runtime 执行](modules/runtime-execution.md) | Run、Runner、Prompt、等待、恢复和结算 |
| [Runtime 调度与数据库并发](modules/runtime-scheduling.md) | ExecutionQueue、Outbox、Worker 租约和推进权 |
| [执行事件与可观测性](modules/observability.md) | ExecutionEvent、Checkpoint 与 OTel Trace |

## 第一阶段技术设计

- [技术设计索引与已确定选型](technical/README.md)
- [第一阶段物理架构](technical/architecture.md)
- [核心数据库 Schema](technical/database-schema.md)
- [状态机](technical/state-machines.md)
- [事务、一致性与并发](technical/consistency-concurrency.md)
- [执行事件、平台审计与可见性](technical/events-audit-visibility.md)
- [Runtime 详细流程](technical/runtime-flows.md)
- [三模式 Agent Harness 稳定性优化方案](technical/agent-harness-stability-plan.md)
- [Context、History 与 Memory 实现](technical/context-history-memory.md)
- [Platform Tool 与 Skill 接口](technical/tools-skills.md)
- [API 与 SSE 契约](technical/api-sse.md)
- [前端详细设计](technical/frontend.md)
- [代码审计与迁移实施计划](technical/audit-migration-plan.md)
- [第一阶段交付状态与验收证据](technical/phase-one-delivery-status.md)
- [第一阶段生产运行手册](operations/phase-one-runbook.md)

## 验收报告

- [首次测试基线：一期真实业务 UAT 报告（2026-08-03）](acceptance/phase-one-uat-2026-08-03.md)
- [修复后持久 UAT 复验报告（2026-08-03）](acceptance/phase-one-uat-revalidation-2026-08-03.md)
- [Harness 修复实现与候选验收报告（2026-08-03）](acceptance/phase-one-harness-remediation-2026-08-03.md)
- [验收文档与证据索引](acceptance/README.md)

## 维护规则

- 一个术语只在其所属模块完整定义；其他文档只做摘要并链接。
- 权威文档只描述目标设计，不记录代码完成度。
- 尚未达成决策的主题进入 `pending/`，不得用模糊措辞写成正式承诺。
- 新设计先验证是否满足 [设计哲学](DESIGN_PHILOSOPHY.md)，再进入所属模块。
- 文档和代码的完成度评估应另行形成审计报告，不反向污染目标设计。
- 现有代码差距和处置只写入技术审计文档，不写入产品权威模块。
