# 第一阶段技术设计索引

本目录是 MyClaw 第一阶段实现级技术设计。产品语义仍以 [设计文档索引](../README.md) 所列产品模块为准；本目录只定义如何实现，不改变产品行为。

## 技术模块

| 文档 | 唯一负责定义 |
| --- | --- |
| [物理架构](architecture.md) | 进程边界、单机拓扑、存储职责、Outbox Topic 与 MQ 演进接口 |
| [数据库 Schema](database-schema.md) | 表、字段、主外键、约束、索引、版本与保留 |
| [状态机](state-machines.md) | 领域对象的合法迁移、事件、并发与迟到结果 |
| [事务、一致性与并发](consistency-concurrency.md) | 原子写入、幂等、租约、fencing、sequence、Workspace 锁和竞态 |
| [执行事件、平台审计与可见性](events-audit-visibility.md) | 两类事件边界、事件 dedupe、visibility、统一投影、脱敏与审计访问 |
| [Runtime 详细流程](runtime-flows.md) | Chat、Plan、Group、Prompt、模型循环、等待、恢复与结算 |
| [三模式 Agent Harness 稳定性优化方案](agent-harness-stability-plan.md) | UAT 系统性失效分析、Harness V2、模型调用稳定性、三模式迁移与发布门禁 |
| [Context、History 与 Memory](context-history-memory.md) | 首批 Policy 接口、默认参数、摘要、投影、记忆和 Trace |
| [Platform Tool 与 Skill](tools-skills.md) | Registry、Invocation、内置 Tool 契约、Skill 包和共享同步 |
| [API 与 SSE](api-sse.md) | HTTP 资源、错误、幂等、分页、权限、冲突和断线续读 |
| [前端详细设计](frontend.md) | 页面、客户端状态、交互契约、后台执行和现有页面处置 |
| [代码审计与迁移实施计划](audit-migration-plan.md) | 当前实现差距、处置矩阵、九阶段顺序、验收和测试 |
| [第一阶段交付状态与验收证据](phase-one-delivery-status.md) | 实现缺口闭环、七个验收场景、最终测试证据、风险与剩余缺口 |

生产发布、备份恢复、Retention 与事故处置见[第一阶段生产运行手册](../operations/phase-one-runbook.md)。

## 已确定技术选型

- MySQL 8 为第一阶段权威数据库，运行事务隔离基线为 `READ COMMITTED`；使用 SQLAlchemy 2 Async 与 Alembic。
- 所有公开 ID 由应用生成 UUIDv7，数据库类型统一 `CHAR(36)`；时间统一 UTC、`DATETIME(6)`。
- 第一阶段采用 Database Outbox，不使用 Celery/Redis 作为可靠事实源；Redis 是实时 Token Bridge 的必需但非权威依赖，故障只降级流式体验。
- SSE 从 `execution_event` 续读；流式模型 Token 经 Redis Pub/Sub 实时转发、不逐 Token 入账，也不重放。
- Session Workspace、Skill 包源和共享 Skill Workspace 使用服务端文件系统；路径只以逻辑相对路径和 URI 进入数据库。
- JSON 只保存不可自然关系化的参数、Schema、快照和结果信封。Agent 装配、图、参与者、依赖、Invocation 与状态均关系化。
- 已确认当前不存在必须保留的生产数据；实施阶段创建全新第一阶段 Schema 基线并重建开发/测试数据库。本设计任务不执行任何数据库操作。

## 跨模块不变量

- Session 执行状态、对应 `ExecutionEvent` 和后续 `OutboxCommand` 同事务提交；无 Session 的管理面变化改写 `PlatformAuditEvent`，不虚构 Session。
- Chat 新 Run 解析最新已发布 AgentVersion；Plan/Group 在图确认或新 GroupRun 开始时固定参与版本。
- Skill 在每个新 Runner 启动时解析最新已发布版本并固定 checksum；SessionSandbox 在 Session 创建时固定。
- Model、Platform Tool 的当前停用状态与 Tool 当前 Confirmation Policy 在真正使用前重新校验。
- Runtime 不补 Tool、不换模型、不修改冻结图、不静默 fallback。
- 同一 WorkItem 最多一个 active Runner；Runner 结束后不可复活。
- Run 是 Chat/Plan/GroupRun 唯一执行 lifecycle/outcome 权威；Plan 只另存定义状态，GroupRun 不复制状态。

## 已确认 Group 后续执行规则

用户已于 2026-07-31 确认：固定 GroupSession 的节点集合继续保留，不因 Agent 停用而替换、删除或静默忽略节点。每次新建 GroupRun 时重新检查固定节点：当前可用节点固定其最新已发布 AgentVersion；停用节点写 `participation_status=unavailable,agent_version_id=NULL`，且不得接收新的 WorkItem、定向消息或 AgentInvocation。非 owner 节点不可用不阻塞其他可用节点继续。

本轮 owner 必须可用。未显式指定 owner 时使用入口节点；入口节点不可用则拒绝创建，并要求用户选择另一个可用固定节点或新建 Group。Agent 后续重新启用只影响再下一次创建的 GroupRun，不改变已创建 GroupRun 的冻结映射。该产品行为已经封版，第一阶段技术设计没有剩余产品确认阻塞项。
