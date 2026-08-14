# MyClaw 设计哲学与产品原则

本文是 MyClaw 权威设计的最高约束。任何模块、技术方案和实现都必须能说明自己如何增强 Agent 的能力，或如何守住不可交给概率模型的物理边界。不能用传统工作流换一套 Agent 名词后进入核心架构。

## 1. 用户定义目标，Agent 决定路径

用户主要通过自然语言说明想解决的问题、重要背景、约束和期望结果。模型负责理解目标、识别歧义、决定是否规划、选择行动、观察结果、调整策略、寻找协作者并完成交付。

平台不能要求用户预先编排每一步，也不能把模型第一次理解冻结成唯一合法流程。只有用户明确要求固定流程，或业务本身是确定性流程时，才应使用 Workflow；开放问题默认交给 Agent Loop。

## 2. Agent 是持久主体，不是常驻进程

一个 Agent 由以下部分共同形成：

```text
Model
+ Instructions
+ Context / Memory
+ Tools / Skills
+ Runtime Environment
+ Identity / Authority
```

Agent 可以代表个人、岗位、业务域或企业公共能力。它有稳定身份、专业边界、工作习惯、能力装配、记忆和结果责任，而不是中央 Orchestrator 中的一段 Prompt、一个固定步骤或一个必须常驻的进程。

必须区分四层：

```text
Agent        = 持久逻辑身份和治理边界
AgentVersion = 不可变配置蓝图
WorkItem     = 跨等待持续的工作状态
Runner       = 某段连续执行所使用的临时运行单元
```

同一 AgentVersion 可以产生多个 Runner；它们仍属于同一 Agent，除非平台显式创建了不同 `agent_id` 和独立数据边界。历史执行引用实际版本，Agent 的成长不能改写历史。

## 3. 一个通用 Agentic Loop

Chat、Plan、Group 共用同一认知与执行循环：

```text
Observe → Understand → Plan when useful → Act → Observe → Adapt → Respond or Wait
```

模型可以在任何模式中产生、更新或放弃计划。平台只传递真实环境反馈，不规定模型必须先调用哪个 Tool、必须经过几个步骤或必须按哪张图运行。

## 4. 模式是策略，不是三套 Runtime

- **Chat**：Agent 可以自主规划并在现有权限内直接行动；
- **Plan**：同一个 Agent Loop 先处于调查和只读策略，用户批准方案后在原 Session 继续实施；
- **Group**：同一个 Loop 增加 Agent 发现、邀请、消息和共享协作能力，多个完整 Agent 各自运行。

模式只改变交互方式、权限检查点、协作关系和信息可见性，不改变 Agent 是否能够规划、使用 Tool、等待、恢复和交付。

## 5. Plan 属于 Agent，不属于编排器

Plan 是 Agent 对当前工作状态的可见投影，用于保持方向、向用户解释进度和跨上下文继续工作。它可以存在于 Chat、Plan 或 Group 中。

Working Plan 必须允许模型根据 Tool 结果、失败、用户反馈和协作者意见动态修订。平台可以保存版本、状态和显示顺序，但不能把 Plan 编译为自动驱动业务的冻结 DAG。

显式 Plan 模式的价值是“实施前检查点”：调查和规划可以继续，写入或高影响行动在用户批准前被权限策略阻止。批准改变的是行动权限，不是把计划转换成 Workflow。

## 6. Group 是工作网中的逻辑关系

Group 成员来自 Enterprise Agent Network。正常 Chat 会把治理过滤后的 Group Agent `id/name/description` 候选目录作为数据交给模型，模型按用户诉求选择最小必要成员；Chat 完成后只让该选择涌现为 GroupRun。Group 不需要角色模板，也不在内部复制 Agent。

Group 保存的是 Membership、Message、Invocation、共享事实和权限边界，不要求成员进程始终在线。成员收到需要处理的消息或调用时，平台才为相应 WorkItem 调度 Runner；等待或空闲时可以没有任何 Runner。

GroupSession 的未来成员可以在后续 Chat 重新发现；每个 GroupRun 固定当时的成员和 AgentVersion 快照。固定的是一次运行的参与事实，不是 Group 的永久本体。

长期允许成员加入、退出、替换和重组。平台记录成员事件、共享范围、权限和成本；是否需要用户确认由当前 Collaboration Policy 决定，而不是由固定模板决定。

## 7. 平台提供可信物理边界

平台只垄断不能交给概率模型的事实：

- 身份、租户、数据隔离和授权；
- Tool、凭证、Sandbox 和外部副作用治理；
- 持久化、幂等、租约、fencing、取消和恢复；
- 预算、并发、风险和用户确认；
- Tool/Agent 调用结果、Artifact 与执行历史的真实性；
- Context 来源、可见性、版本与完整性。

平台不决定领域步骤、协作顺序、重试策略或专业结论。模型也不能自行扩权、伪造执行结果、改写历史或突破硬预算。

## 8. 能力稳定，副作用精确治理

Tool、Skill、Model、Sandbox、Agent、Context、Memory 和 Policy 相互正交。Skill 或自然语言不能产生 Tool、Agent 或数据权限；发现 Agent 也不等于允许调用。

有效能力来自机器态交集。只要授权条件没有改变，某个 Tool 成功使用一次后仍然可用。是否允许重试或重复副作用必须按具体 Invocation、参数、目标资源、业务意图和 operation key 判断，不能消费整个 Tool 类型。

## 9. Context Engineering 是 Harness 核心

Harness 的重要职责不是写越来越长的系统 Prompt，而是让每次模型调用获得最小且高信号的上下文：当前用户输入、有效能力、必要历史、未处理结果、工作计划、共享事实、预算和环境状态。

长任务通过 Working Plan、进度摘要、Artifact、Invocation 结果、文件和 Checkpoint 跨上下文延续。原始 Tool 日志和过期中间内容应被引用或压缩，避免 Context 污染。

## 10. Turn 停止、任务完成和验收相互独立

Provider 的普通最终消息或 `end_turn` 只表示模型结束当前 Turn：

- 根 Agent 的最终消息交付给用户；
- 被调用 Agent 的最终消息返回调用方；
- Group 成员的最终消息进入定向或共享记录；
- 等待用户、Tool 或 Agent 时，Run 保持可恢复等待。

平台不要求模型调用 `finalize_response`，也不要求模型声明 `required_tool_keys`。Run 是否仍有 pending action、是否被取消、是否预算耗尽，由机器事实决定。

“结果已经被验证”只能来自真实测试、Tool 结果、Artifact 或业务系统状态。确定性 Gate 可以控制 `verified` 标签或高风险发布动作，但不能伪装成 Agent 的通用完成协议。

## 11. Evaluator 是可选能力

独立 Evaluator 只在以下条件同时成立时使用：

- 评价标准明确；
- 单纯确定性检查不足；
- 迭代反馈被证明能提升结果；
- 成本和误判可以接受。

Evaluator 可以作为专项 Agent、评审 Tool、Stop Hook 或离线 Eval 存在，不能成为所有 Chat、Plan、Group 的强制结算节点，也不能拥有额外业务权限。

## 12. 专业性来自 Agent 装配与成长

专业 Agent 的差异主要来自 Model、Instructions、Skill、Tool、知识、记忆和历史经验。个人 Agent 的性格、耐心、严谨程度和协作习惯应由这些可演进内容塑造，并通过实际行为和结果观察。

平台可以提供可选的发现标签或行为摘要，但不应用一组强制枚举替 Agent 定义性格。Group 也不能用多个空洞角色 Prompt 掩盖单体 Agent 能力不足。

## 13. 协作是可寻址的社会行为

Agent 间协作应形成显式的发现、邀请、消息、请求和结果事实。调用方说明目标和必要上下文，目标 Agent 保持自己的身份、能力和专业判断，可以接受、追问、反提议、拒绝或返回结果。

Chat/Plan 中的委派通常把结果返回当前责任 Agent；Group 成员可以直接通信、共享工作状态并接受用户定向交互。Instructions、私有记忆、凭证、完整 Prompt 和原始 Chain-of-Thought 不共享。

同一 MyClaw 控制域内，Agent 通过稳定 ID、数据库目录和调度器完成本地寻址，不需要为每个 Agent 分配网络地址或常驻服务。跨 MyClaw 域时才由 Gateway 把相同的 AgentInvocation 语义映射到外部发现、身份和通信协议。内部优化不能绕过统一授权和审计边界。

## 14. 人掌握目标、授权和高影响决定

人不需要持续充当工作流调度器，但必须能：

- 修正目标和重要约束；
- 在 Plan 模式批准实施；
- 批准高风险副作用和越界协作；
- 随时查看、打断、暂停、取消或继续；
- 直接与 Group 任一成员交互；
- 对价值冲突、责任和高影响结果作最终决定。

## 15. 执行可恢复，历史不可伪造

Session 可以长期持续；Run 表达一次用户请求或协作工作；Turn 是一次模型决策；WorkItem 保存跨等待工作；Runner 是一次短生命周期执行片段。Runtime Worker 是承载 Runner 的计算进程，不属于 Agent 身份。

等待用户、审批、Tool 或 Agent 时结束当前 Runner，条件满足后以新 Runner 从权威事实继续。技术重试不能重新创造业务 Invocation。终态 Run 不重新激活，后续工作创建引用历史的新 Run。

## 16. 可观察的是决定和事实，不是私有思维链

系统至少应回答：

- 使用了哪个 AgentVersion、Model、Tool、Skill 和权限快照；
- 模型产生了哪些可见计划更新、Action 和协作；
- 平台为什么允许、拒绝、等待或重试；
- 真实产生了哪些结果、文件、测试和副作用；
- 当前为何仍在工作、等待、失败或结束。

这些解释来自消息、计划、Invocation、Artifact、环境状态和决策摘要，不依赖持久化或共享原始 Chain-of-Thought。

## 17. 简单性是架构约束

任何新增实体、状态机、Evaluator 或中间协议必须证明它解决了无法由现有 Agent Loop、权限、Invocation、Context 或确定性检查解决的问题。

尤其禁止：

- 为了“看起来工程化”增加强制完成信封；
- 用模型生成的 JSON 代替自然交互；
- 为 Group 预建角色模板和固定拓扑；
- 把一期 UAT 限制写进终局产品本体；
- 为每个模式复制一套状态和执行代码；
- 用不可观测的隐式 fallback 掩盖 Provider 或配置错误。

## 18. 架构判断顺序

每个设计决策依次回答：

1. 这件事能否由 Agent 根据上下文自主决定？
2. 如果不能，是否因为身份、权限、副作用、持久化或物理事实必须由平台保证？
3. 能否通过现有简单原语解决，而不是增加新领域对象？
4. 该限制是终局产品规则，还是一期验证策略？
5. 是否保留了模型观察结果、修正计划和恢复工作的能力？

无法通过这五问的设计，不应进入权威架构。
