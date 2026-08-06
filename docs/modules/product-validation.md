# 产品价值验证

本文是 MyClaw 目标用户、替代方案、价值假设、成本口径和对照实验的唯一权威来源。它不定义 Runtime 语义，也不把架构可行性当作商业价值证据。

## 当前判断

```text
architecture hypothesis: coherent enough for controlled implementation/UAT
problem/solution fit: unproven
repeatable enterprise value: unproven
commercial readiness: unproven
```

MyClaw 的方向是“可治理的企业 Agent 工作网 + 持久 Harness”，但客户是否愿意迁移、使用和付费，必须由真实任务、采用行为和单位经济证明。文档完整、UAT 演示和多 Agent 看起来热闹都不能单独证明价值。

## 目标用户与购买者

### 1. 企业 AI / 平台负责人（主要购买者）

需要在一个治理面上发布、运行、观察和控制多个 Agent，同时保留模型/工具选择、身份、审计和私有化边界。

待验证痛点：团队各自拼接 Agent SDK，导致身份、版本、Tool 治理、恢复和可观测性重复建设；集中平台又可能把开放任务强制写成固定工作流。

成功信号：愿意提供真实系统接入、指定平台 Owner、把第二个业务 Agent 迁入同一控制面，并为治理/运行能力付费。

### 2. Agent Builder / 业务能力 Owner（主要生产者）

把领域 Instructions、Model、Tool、Skill、Memory Policy 和环境装配成可发布 Agent，希望不用维护独立服务和 Harness。

待验证痛点：开发到生产之间缺少可复现版本、真实权限、持久等待、测试和反馈闭环。

成功信号：从想法到通过业务验收的发布时间下降；发布后能从失败证据定位并发布新版本，而非复制 Prompt 或改线上配置。

### 3. 知识/运营/研发工作者（主要使用者）

希望直接把跨步骤目标交给 Agent，在关键点确认，离线后继续，并能判断交付是否真的执行和验证。

待验证痛点：单轮聊天需要反复搬运上下文；多系统操作不可恢复；不同专业 Agent 之间靠人手工转述。

成功信号：accepted outcome 时间和人工协调时间下降，用户在第二周仍主动使用，并能解释为什么信任或不信任结果。

### 4. 安全、合规与运维（关键否决者）

关心谁代表谁调用、数据流向、凭证、成本、失败恢复、审计和停机处置。他们未必是日常用户，但可以否决生产发布。

成功信号：威胁模型与生产 Gate 获批，审计能回答真实事件而不暴露 Prompt/Secret，故障演练不产生重复副作用。

## 优先 JTBD

第一阶段只验证一个窄 JTBD：

> 当一个小型软件交付目标需要产品、开发、测试专业判断和真实文件/执行证据时，我希望在同一受控空间交给一个 Agent 或多个 Agent 推进，在必要时批准，并得到可追溯的最终交付，从而减少人工转述和重复协调。

容量风险计算器是验证夹具，不是目标垂直市场。进入设计伙伴阶段前，必须从访谈中选出一个有频率、预算和系统 Owner 的真实业务流程，不能长期用自造 Demo 代替客户问题。

## 替代方案与行业参照

以下只基于 2026-08-06 可访问的官方资料描述产品类别，不构成完整竞品评分或采购建议：

| 类别 | 代表性官方资料 | 客户为什么会选它 | MyClaw 必须证明什么 |
| --- | --- | --- | --- |
| 可嵌入 Agent SDK / Sandbox Harness | [OpenAI Agents SDK](https://openai.github.io/openai-agents-python/)、[Sandbox Agents（Beta）](https://openai.github.io/openai-agents-python/sandbox_agents/) | 除 Agent loop、Tool、handoff、session 和 tracing 外，已提供隔离工作区、文件/命令能力、snapshot/session state 与任务恢复；团队仍保留应用代码控制 | MyClaw 的持久业务 Run/Outbox、企业控制面、版本/身份治理和 Agent Network 是否值得采用完整平台 |
| Framework + Opinionated Harness + Workflow | [Microsoft Agent Framework](https://learn.microsoft.com/en-us/agent-framework/overview/)、[Agent Harnesses](https://learn.microsoft.com/en-us/agent-framework/agents/harness)、[Google ADK](https://docs.cloud.google.com/gemini-enterprise-agent-platform/build/adk) | 已覆盖动态 Agent、Harness loop、计划/todo、Compaction、文件/Memory、Approval、Observability，以及需要确定顺序时的 Workflow | MyClaw 不能把基础 Harness 当差异化；必须证明统一企业工作网、持久协作和私有治理的净价值 |
| Managed Harness / Runtime | [Amazon Bedrock AgentCore Harness 与 Runtime](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-vs-runtime.html) | 配置化 Agent、托管运行环境、身份、扩缩、文件/Memory/可观测性，减少基础设施自建 | 私有控制面、模型/环境中立、逻辑 Agent 网络和可治理 Group 是否形成足够差异 |
| 企业低代码 Agent 平台 | [Microsoft Copilot Studio](https://learn.microsoft.com/en-us/microsoft-copilot-studio/fundamentals-what-is-copilot-studio)、[Salesforce Agentforce](https://help.salesforce.com/s/articleView?id=ai.copilot_intro.htm&language=en_US&type=5) | 已有数据、连接器、渠道、权限、低代码 Builder 和采购关系 | 不依赖特定 SaaS 数据栈时的部署/治理价值，以及更开放的 Agent-to-Agent 模型 |
| 现有自动化/工作流/RPA | 客户当前系统 | 确定性、可审计、团队已熟悉；开放推理并非所有任务都需要 | 在不牺牲控制的前提下解决真正开放、变化和高上下文任务 |
| 人工协作 + 通用聊天模型 | 客户当前操作 | 灵活、无需平台迁移；人承担协调和最终责任 | 明确减少 accepted outcome 时间、人工转述和返工，而不是只增加 Token 与等待 |

任何详细功能/价格矩阵都必须注明 `last_verified_at`、版本/成熟度（GA、Preview、Beta）、地域和官方来源。至少每季度、每次产品定位评审以及重大采购/路线决策前重新核验；发现代表性产品跨越分类边界时直接调整分类，不维护过时标签。厂商自述只能证明其公开定位，不能替代客户实测。

## 差异化假设，不是已证明卖点

1. **Agent 是稳定逻辑主体**：配置可发布为不可变版本，计算按需出现，不要求每 Agent 一个常驻服务；
2. **一个持久 Harness**：Chat、Plan、Group 共享 Agent Loop、WorkItem、Invocation、等待和恢复；
3. **Plan 是权限模式**：开放任务由模型动态规划，高影响实施保留人类检查点；
4. **工作网与 Group**：AgentCard 发现、Membership、对等消息和 Invocation 支持从本地数据库演进到联邦 Gateway；
5. **事实与表达分离**：普通消息自然结束，Tool/Artifact/Verification 决定可验证事实；
6. **治理证据内建**：内部 account、实际认证 caller、CapabilitySnapshot、版本、Outbox、fencing、Artifact 和审计形成统一执行证据；一期 raw userId 风险仍需在生产前关闭。

每条假设都需要一个可证伪实验。若同样价值可由现有 SDK/平台以更低总成本获得，应缩小 MyClaw 范围或重新定位，而不是增加更多抽象。

## 北极星与护栏指标

主指标：

```text
accepted_outcome_rate
= 被业务 Owner 接受且无隐藏关键缺口的 Run / eligible Runs
```

配套价值指标：

- time_to_accepted_outcome；
- human_active_minutes 与人工转述次数；
- first-pass acceptance、rework rounds、abandonment；
- verified claim rate 与 false-verified incident；
- 恢复后完成率、重复副作用、权限违规；
- 第 2/4 周有意义复用、第二个 Agent/流程采用；
- Builder 从 draft 到 accepted release 的周期。

护栏指标：

- 端到端 p95/p99、waiting age；
- 模型/Tool/Sandbox/存储成本；
- 平均 Turn、Token、Agent fan-out 和 Invocation depth；
- user intervention、approval fatigue、clarification burden；
- failed、cancelled、outcome_unknown、人工接管；
- 安全、隐私、合规和 support incident。

不得只优化 Run completion rate：模型可以产出文字并 completed，但结果未被接受或产生过高成本。

## 单位经济

统一按“被接受的结果”计算：

```text
cost_per_accepted_outcome =
  (model tokens
   + model requests
   + tool/API fees
   + sandbox compute
   + storage/egress
   + platform operations
   + human review/intervention
   + failed/retry/incident allocation)
  / accepted outcomes
```

同时记录边际成本和全成本。Group 的成本必须包含所有成员、汇总、共享检索和失败返工，不能只计最终 Reporter。对企业客户还要记录部署、连接器、治理和升级的人力成本。

价格或包装只有在明确成本分布、业务价值和付费主体后决定。不得先用“Agent 数”“消息数”作为定价单位，再倒推客户价值。

## 多 Agent 必须通过对照实验

对同一任务使用同一数据、模型等级、Tool、权限、时间上限、上下文和可接受预算，随机或交叉执行：

| Arm | 机制 | 回答的问题 |
| --- | --- | --- |
| A | 单 Agent | 一个完整 Agent 是否已经足够 |
| B | 单 Agent + 按需 delegation | 专业子任务回到责任 Agent 是否改善结果 |
| C | Group 对等协作 | 直接交流、分歧和复核是否进一步增加价值 |
| D | 确定性 workflow / 人工现状（适用时） | 该任务是否根本不需要动态多 Agent |

主比较使用 blind business acceptance；同时测质量、verified evidence、时长、人力、成本、失败率和尾延迟。样本按任务复杂度/风险分层，预先定义停止条件，不能只展示成功轨迹。

Group 只有在目标区间内持续满足以下条件才可作为推荐默认：

```text
quality/value lift > agreed threshold
AND incremental cost/latency <= agreed budget
AND safety/reliability guardrails not worse
```

否则默认单 Agent 或 delegation。多 Agent 是可选择的执行策略，不是产品价值本身。

## 验证阶段与退出条件

### Stage 0：问题访谈

- 至少覆盖购买者、Builder、使用者和否决者；
- 收集真实最近一次任务、频率、损失、现有替代和采购边界；
- 退出：选定一个有数据 Owner、系统权限和复现样本的窄场景。

### Stage 1：受控 UAT

- 验证当前架构机制、身份/权限、恢复和证据诚实性；
- 对照 A/B/C/D，不只跑 Group；
- 退出：达到预先登记的 acceptance、成本、时间和安全门槛。

### Stage 2：设计伙伴 Pilot

- 接入真实非关键业务数据和 Tool；
- 连续观察采用、人工接管、失败和成本；
- 退出：客户主动复用、增加第二个流程/Agent，并由业务 Owner 确认净价值。

### Stage 3：Production readiness

- 完成[企业生产就绪要求](../operations/production-readiness.md)；
- 退出：同一 release 的 SLO、容量、HA、RPO/RTO、安全、运维和数据治理 Gate 全部通过。

### Stage 4：商业验证

- 证明可重复部署、支持成本和购买流程；
- 退出：多个独立客户/部门达到付费、续用或等价的明确承诺。

## 当前必须保持诚实的表述

- 可以说：设计方向与主流 Agent loop/harness 原语相容，第一阶段可做受控验证；
- 可以说：工作网、持久执行和对等 Group 是待验证差异化假设；
- 不可以说：架构完整等于企业生产就绪；
- 不可以说：多 Agent 一定优于单 Agent；
- 不可以说：厂商有类似功能就证明市场属于 MyClaw；
- 不可以说：现有 UAT 已证明商业价值或产品市场匹配。
