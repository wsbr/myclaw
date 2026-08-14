# MyClaw 产品设计总览

> 本文是产品入口，只概述目标体验。完整定义以[设计文档索引](docs/README.md)中的权威模块为准。

## 产品定位

MyClaw 是 Agent-native 的云端 Agent 平台。个人、岗位、业务域和企业公共 Agent 先成为能够独立工作的专业主体，再作为节点进入企业工作网，按目标发现彼此、临时协作并持续成长。

用户用自然语言表达想解决的问题。模型驾驭 Agent，负责理解、规划、行动、观察、调整、协作和交付；平台负责身份、权限、上下文、Tool、Sandbox、持久状态、副作用、预算和可观测性。

长期目标是可治理的动态 Agent 社会，不是中央超级 Agent、角色模板市场或固定工作流平台。完整北极星见[Enterprise Agent Network](docs/modules/enterprise-agent-network.md)。

## 核心产品对象

- **Agent**：稳定的逻辑工作主体和治理身份，不等同于配置行或常驻进程；
- **AgentVersion**：Agent 的不可变发布配置，固定 ModelVersion、Instructions、Tool 选择/registration hash、精确 SkillVersion/checksum、PolicyVersion 和 SandboxProfileVersion；Tool 的实时启停、确认、风险和连接治理不冻结；
- **AgentCard**：Agent 在工作网中的安全发现信息；
- **Enterprise Agent Network**：Agent 被发现、选择、邀请和建立信誉的网络；
- **UserSession**：用户与一个 Agent 或 Group 持续交互的上下文；
- **Run / WorkItem**：一次用户工作及可跨等待持续的工作状态；
- **Turn / Runner**：一次模型决策及承载若干决策的短生命周期执行片段；
- **WorkingPlan**：Agent 在复杂任务中按需维护的可见工作计划；
- **ToolInvocation / AgentInvocation**：真实 Tool 行动和 Agent 协作；
- **Clarification / Approval**：可以跨时间恢复的人类输入与高影响确认；
- **GroupSession / Membership / GroupRun**：正常 Chat 中由模型从治理后的 Agent 语义目录按诉求选员、Chat 后涌现的协作关系；GroupRun 的状态由一对一 Run 统一承载；
- **Workspace / Artifact**：分区的运行工作区与不可变正式交付快照；
- **ExecutionEvent / Trace**：可靠业务时间线和诊断数据。

这些对象服务于 Agent，而不是替 Agent 决定业务步骤。

## 装配一个完整 Agent

Agent 的专业性来自整体装配：

1. **身份与职责**：代表谁、服务谁、擅长什么、不负责什么；
2. **Instructions**：领域原则、工作方法、协作习惯和交付标准；
3. **Model**：与任务复杂度、成本和延迟匹配的模型；
4. **Tool 与 Skill**：真实行动能力和可复用专业方法；
5. **Context 与 Memory**：当前信息、长期偏好和领域事实；
6. **Sandbox 与业务连接**：Agent 能观察和改变的环境；
7. **AgentCard**：向工作网公开的能力、边界、成本和可用性摘要。

个人 Agent 的耐心、严谨、沟通风格和协作习惯主要写入 Instructions、Memory 或 Skill，并通过运行行为观察。平台可以提供可选标签，但不使用固定 Profile 枚举替 Agent 定义人格。

Agent 使用草稿—发布模型。发布版本不可变，运行记录实际使用的版本；Builder 拥有 Agent 蓝图，不因此拥有使用者数据。

Agent 平时不需要占用进程。收到用户请求、AgentInvocation 或 Group 消息后，平台为对应工作创建或恢复 WorkItem，再由 Runtime Worker 加载 AgentVersion、上下文和权限运行一个 Runner。Runner 终结不影响 Agent、Session、Membership 和历史继续存在。同一 AgentVersion 的并发 Runner 默认是同一 Agent 的多个执行实例，不是自动复制出的新 Agent。

## 自然语言驱动的通用 Agent Loop

```text
用户提出目标
    ↓
Agent 读取当前上下文和有效能力
    ↓
判断是否需要澄清、规划、使用 Tool 或寻找 Agent
    ↓
执行行动并观察真实结果
    ↓
调整理解和 WorkingPlan
    ↓
继续行动、等待输入或自然交付
```

简单请求可以直接回答；复杂请求可以在普通 Chat 中自主产生计划并按计划执行。用户不需要切换模式才能获得模型的规划能力。

## Chat、Plan、Group

三个模式共用同一个 Agent Loop，区别只在交互、权限和协作策略。

| 维度 | Chat | Plan | Group |
| --- | --- | --- | --- |
| 核心体验 | 与一个 Agent 连续工作 | 实施前先调查和确认方案 | 多个 Agent 直接协作 |
| 是否可以规划 | Agent 按需规划 | 必须先形成可见方案 | 每个成员都可规划 |
| 实施权限 | 按当前授权直接行动 | 批准前只读，批准后执行 | 每个成员按自身权限行动 |
| 协作关系 | 当前 Agent 可按需委派 | 当前 Agent 可研究和委派 | 成员对等通信与协作 |
| 用户交互 | 当前 Agent | 当前 Agent | 可直接面向任一成员 |

### Chat

Chat 是默认工作方式。Agent 可以直接回答，也可以自主拆解复杂任务、维护 WorkingPlan、使用 Tool、请求澄清或调用其他 Agent。协作结果回到当前 Agent，由它继续面向用户工作。

Chat 不是“只能聊天且不能规划”的简化模式。

### Plan

Plan 是实施前检查点，而不是独立工作流引擎。进入 Plan 后，Agent 可以阅读、搜索、分析、向用户澄清并生成 WorkingPlan，但写文件、执行高影响命令和外部副作用保持禁止。

用户可以修改或继续讨论方案。批准后原 Session 切换到可执行权限，Agent 基于当前方案继续工作，并可以随新信息动态修订计划。批准的是“允许开始实施”，不是把计划冻结成 DAG。

### Group

Group 是工作网中多个完整 Agent 围绕当前目标形成的临时逻辑协作关系。成员不需要保持常驻进程；消息或调用到达时，平台按需运行相应 Agent。

用户先进行正常 Chat。root Runner 把原身份与治理边界内全部已开启 Group 且当前可用 Agent 的 `agent_id/name/description` 作为候选数据交给模型；当前 Chat Agent 保留为会话锚点，模型按用户诉求选择最小必要协作成员，首轮回答成功提交后形成涌现式 Group。Group 不依赖产品/开发/测试之类的角色模板，也不预生成固定协作边。成员加入后保留自己的身份、Instructions、Memory、Tool 和专业判断，可以直接通信、认领工作、质疑、返工或向用户询问。

GroupSession 的未来成员随每次 Chat 按最新 AgentCard 与 Collaboration Policy 重算；每个 GroupRun 仍固定当时的成员与 AgentVersion 快照。旧版手选三人 Group 只作为历史兼容路径保留。

每个 GroupRun 固定一名 Reporter；源 Chat Agent 被模型选中时优先由它担任，否则使用模型选择中的首位 Agent。Reporter 只在所有持久工作自然空闲后负责汇总，不获得额外权限或 Supervisor 地位；平台通过持久事件幂等唤醒，避免“大家都结束了但没人向用户交付”。

## Plan 是通用工作状态

WorkingPlan 可以在三个模式中出现：

- 普通 Chat 遇到复杂目标时自动出现；
- Plan 模式中作为实施前方案；
- Group 中每个成员可以维护自己的计划，Group 也可共享任务或里程碑。

计划可以包含步骤、状态、依赖、风险和变更原因，但它只是高信号工作记忆和用户可见进度。平台不根据计划内容自动创建 Tool 调用或业务任务。

## 工作网与 Group

在同一 MyClaw 平台内，AgentVersion 发布后生成 AgentCard 安全投影并写入平台目录。工作网发现本质是受权限过滤的数据库查询，不需要为每个 Agent 建立独立网络服务：

```text
AgentVersion → AgentCard → Enterprise Agent Network
                              ↓
                 Chat 模型按诉求语义选员
                              ↓
                    Chat 完成后形成 Membership
                              ↓
                         GroupSession
```

固定 Group 成员不是代码写死，也不是从模板实例化，而是某次 GroupRun 对真实网络 Agent 的可审计快照。

`group_enabled` 只表示 AgentVersion 可以成为 Group 候选。实际查询仍要过滤发布状态、租户与用户可见范围、协作策略、环境兼容性和可用性。发现只说明“谁可能适合”，不能自动产生权限；成员加入或调用时平台仍检查原始用户权限、Agent 装配、数据共享范围、预算和风险策略。

未来跨组织或跨 MyClaw 实例时，由 Agent Gateway 增加外部 Endpoint、协议协商和联邦身份。产品层仍使用同一发现、Membership 和 AgentInvocation 语义，不把公网协议复杂度提前复制到平台内部。

## 自然停止和真实验证

Agent 不需要调用 `finalize_response`。当模型返回普通最终消息且没有 Tool Call 时，当前 Turn 自然结束：根 Agent 的消息交给用户，被调用 Agent 的消息返回调用方，Group 成员的消息进入相应会话。

平台区分：

- **Turn 已结束**：模型当前没有继续行动；
- **Run 在等待**：仍等待 Tool、Agent、用户或批准；
- **Run 已结束**：本次请求得到结果、失败、取消或被中断；
- **结果已验证**：真实测试、Artifact 或业务状态满足可机器检查的条件；
- **UAT 已通过**：外部验收用例和统计门禁满足。

模型最终文本不能伪造测试、文件或副作用。平台可以基于权威 Invocation 和环境状态显示 `verified / unverified`，也可以对发布、高风险动作设置确定性 Gate；但不把独立 LLM Evaluator 强制塞进所有任务。

## 可恢复的云端执行

Session 和 WorkingPlan 可以长期存在。等待用户、审批、Agent 或异步 Tool 时，当前 Runner 结束；等待满足后，新 Runner 从 Checkpoint、消息和权威结果继续。

页面关闭、网络断开和 Worker 重建不会丢失任务。技术重试复用同一 Invocation，不创造第二个业务动作。取消停止后续推进，但不承诺回滚已发生的外部副作用。

## 文件、Tool 与环境

Tool 在授权期间持续可见，不因成功一次被消费。是否重复执行由 Tool 语义、参数、目标资源、operation key 和当前业务意图决定。

文件先存在于 Session Workspace 的当前 Agent 私有根或 Group shared 根；物理共享目录不代表所有成员可读。需要正式交付时，平台把源字节复制成不可变 Artifact 快照并记录来源、checksum、版本和下载信息。Local Sandbox 是第一阶段验证环境，不宣传为生产级不可信代码强隔离。

## 用户如何驾驭 Agent

用户可以：

- 用自然语言补充目标、纠正理解和增加约束；
- 查看或修改 WorkingPlan；
- 在 Plan 模式批准或拒绝实施；
- 批准精确 Tool 调用或高风险副作用；
- 查看 Agent 正在使用的身份、版本、能力和环境；
- 在 Group 中选择、邀请或直接联系任一成员；
- 暂停、继续、取消或要求提前汇报；
- 查看真实 Tool 结果、文件、测试、Artifact 和失败原因。

用户不需要手工调度每个步骤，也不需要知道内部 Tool key。

## 验证与成熟度

确定性测试用于验证持久状态、权限、调度、恢复和接口契约；真实模型验收用于验证 Chat、Plan、Tool use 和涌现式 Group 行为。两者不能互相替代，模型输出也不能替代真实 Tool、Artifact 或业务状态。

当前实现范围和限制以 [README 成熟度表](README.md#project-maturity)为准，验证与证据边界见[验证说明](docs/VALIDATION.md)。目标用户、替代方案、价值指标、成本和多 Agent 对照实验见[产品价值验证](docs/modules/product-validation.md)；这些仍是待验证假设，不宣称已证明商业价值。

企业生产不是把第一阶段 Compose 直接换一个部署名称。正式产品 Profile 必须使用受验证的 OIDC/OBO 身份、强隔离 Sandbox、HA 数据与存储、明确 SLO/RPO/RTO 和绑定 release 的演练证据；具体落地合同见[企业生产参考架构](docs/technical/production-reference-architecture.md)与[生产就绪要求](docs/operations/production-readiness.md)。

## 产品底线

- 不用固定工作流代替 Agent 的理解和规划；
- 不把 Chat、Plan、Group 实现成三套 Agent；
- 不强制模型通过 `finalize_response` 或复杂最终 JSON 才能结束；
- 不让模型定义自己必须调用过哪些 Tool；
- 不因一次成功调用删除整个 Tool 能力；
- 不把 WorkingPlan 当作平台可执行 DAG；
- 不为 Group 强制角色模板、固定拓扑或永久 Supervisor；
- 不把验收 fixture 的固定成员写成终局产品限制；
- 不共享 Agent 私有 Prompt、Memory、凭证和 Chain-of-Thought；
- 不让文字声称代替真实 Tool、测试、Artifact 和业务状态。

## 文档导航

- [设计哲学与产品原则](docs/DESIGN_PHILOSOPHY.md)
- [Enterprise Agent Network](docs/modules/enterprise-agent-network.md)
- [Agent 装配](docs/modules/agent-assembly.md)
- [Agent 协作](docs/modules/agent-collaboration.md)
- [Plan 模式](docs/modules/plan-mode.md)
- [Group 模式](docs/modules/group-mode.md)
- [Runtime 执行](docs/modules/runtime-execution.md)
- [验证说明](docs/VALIDATION.md)
- [技术设计总览](TECH.md)
- [企业生产参考架构](docs/technical/production-reference-architecture.md)
- [OpenAPI 3.1](docs/technical/openapi-v1.yaml)
