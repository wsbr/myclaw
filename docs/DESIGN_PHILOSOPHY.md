# MyClaw 设计哲学与产品原则

本文是 MyClaw 权威设计的最高约束。模块设计必须能说明自己如何满足这些原则；需要例外时，应先更新本文件并形成明确决策。

## 1. 装配，而不是重复建设

MyClaw 是垂类 Agent 装配平台，不是单纯聊天产品，也不是以固定流程为中心的通用工作流平台。平台统一提供可复用运行时能力，让装配者专注于领域职责和能力选择。

Agent 是完整、独立、可直接面向用户工作的装配蓝图。稳定 Agent 身份与不可变发布版本分离，历史执行必须能引用实际版本。它不是提示词片段，也不是只能被某个中心 Agent 调用的从属步骤。

长期目标是让个人、岗位、业务域和企业公共 Agent 共同组成可发现、可治理的企业智能体网络。人发起目标，Agent 优先完成工作并按需协作或找人；平台不能把这一网络收缩为一个中央超级 Agent 或固定工作流。

## 2. 能力正交与显式授权

Tool、Skill、Model、Sandbox、Agent、上下文策略和记忆策略相互正交：

- 选择 Skill 不会自动获得 Tool、MCP 或 Agent 权限。
- 绑定 Agent 不会自动获得调用能力。
- 启用 Group 不会自动添加 `invoke_agent`。
- Runtime 不推断“合理依赖”并静默补齐能力。

Agent 的有效能力必须能从其装配配置直接解释。

## 3. 平台实现，Agent 选择

模型连接、Tool 实现、Sandbox Profile、策略实现和 Runtime 机制由平台统一建设和治理。Agent 页面只选择平台已经提供的能力，不重复维护底层参数或实现副本。

Skill 是例外的内容扩展入口：平台允许上传通用、可移植的 Skill 包，但 Skill 仍不能改变 Agent 的授权边界。

## 4. 执行忠于装配

Runner 启动时解析并固定本次实际装配。Runtime 不切换模型、不添加 Tool、不改写 Agent 定义，也不以 fallback 掩盖配置或基础设施错误。

Chat 的新 Run 使用最新发布 AgentVersion，Plan 和 Group 在图确认时固定参与 AgentVersion。Skill 按逻辑标识在新 Runner 启动时解析最新版；已开始的 Runner 使用固定 checksum。Sandbox Profile 在 Session 创建时固定。

## 5. 会话可持续，执行不可复活

Work Item 可以跨时间持续，Runner 只是一次短生命周期执行。等待条件满足后创建新 Runner，不恢复已经结束的进程。

Session 或 Group 上下文可以跨多次执行保留，但进入终态的 PlanExecution 或 GroupRun 不重新激活。后续工作创建新的执行对象并引用历史结果。

取消表示停止继续推进和尽力取消运行操作，不承诺回滚已经产生的业务副作用。

## 6. Agent 平等，模式职责明确

所有 Agent 都是垂类完整 Agent。所谓入口、根节点或被调用 Agent，只描述它在某次任务中的位置，不形成永久等级。

Agent 间只有一个交互通道：显式装配的 `invoke_agent` Tool。平台不提供隐藏消息总线或隐式委派。

Chat 和 Plan 采用中心化严格星形协作，Group 采用对等协作。三种模式共享执行原语，但不相互嵌套，也不混用图语义。

## 7. 图的授权边界必须显式、可审计

当前 Plan 和 Group 都必须先展示图，由用户确认或修改后执行，不提供自动执行策略。

Plan 确认后冻结 Work Item Graph 与执行 Agent 集合；Group 确认后冻结参与 Agent 集合。执行 Runtime 不能暗中扩展确认范围。

未来若引入预授权自动执行或版本化 Group Amendment，必须作为独立产品策略重新确认，并保留明确授权、版本和审计边界。在该设计完成前，Runtime 仍严格执行当前确认与冻结规则。

## 8. Group 是共享会话的临时协作群

Group 围绕一个用户目标建立统一 GroupSession。入口 Agent 生成参与图和待共享 Bootstrap Context；建群前完整 Chat 历史不默认开放。

Group 消息定向处理：只有指定 Agent 被立即唤醒。每个 Agent 的上下文策略只能处理其直接可见范围；其他群组共享记录必须通过显式 Tool 查询。共享会话不等于完整记录自动进入 Prompt，也不等于共享 Instructions、私有记忆、内部 Prompt 或模型推理。

Group 的人类参与者只有任务发起人。垂类 Agent 后期定向联系的岗位工作人员、责任人或审批人属于 Group 外的等待目标，不因此加入群组或获得群组上下文。

## 9. 语义判断与可靠执行分离

Agent 负责领域判断：如何理解目标、需要什么工作、何时需要澄清、产出何种语义结果。

平台负责可靠执行：授权检查、路由、持久化、并发控制、状态机、ExecutionEvent、Checkpoint 和失败暴露。Observability Trace 只负责调试与性能观测。Agent 不能直接修改平台执行数据库。

Agent 调用始终代表原始执行主体，目标 Agent 带来领域能力而不是身份提权。

队列命令只负责触发执行，不成为业务状态事实源。同一 Work Item 的推进权必须唯一，重复投递和 Worker 失联不能造成重复业务推进。

## 10. 轻量确认与开放交互

平台只为风险 Tool 调用提供可恢复的精确确认/拒绝机制。第一阶段由 Platform Tool Registry 配置 `never | always`，Agent 不得覆盖；动态风险判断和垂类审批后置，不能统一写死在平台。

平台前端只是默认客户端。Plan/Group 图确认、Clarification 和 Tool Confirmation 都应具有 API 可表达的状态与响应协议。

## 11. 身份统一，入口不分裂用户

Web、API、Channel 和 Agent 调用链使用同一个企业 `userId`。入口凭证和第三方账号只负责认证与会话绑定，不能创造新的用户数据命名空间。

任务通知默认跟随 Session 来源。Channel 是平台消息适配能力，不是 Agent Tool，也不是隐藏的 Agent 协作通道。

## 12. 可解释优先于魔法

每次 Runner 都应能回答：

- 实际用了哪个模型；
- 可调用哪些 Tool；
- 加载了哪些 Skill 版本；
- 采用了哪些策略；
- 能发现和调用哪些 Agent；
- Prompt 各部分来自哪里；
- 状态和成果如何产生。

不追求用隐式行为营造“智能”，而追求可理解、可治理的装配与协作。

## 13. 失败也是结果

Work Item 成功、失败、超时、取消或放弃都必须形成明确结果。Run 的生命周期完成与业务目标成功是两个维度；部分失败可以被汇总交付，未完成项可以被显式收敛，不能让执行永久卡住。

## 14. 蓝图所有权不等于用户数据所有权

Builder 可以装配和发布 Agent，但不能因此读取使用该 Agent 的用户会话、记忆、Artifact 或业务内容。Agent 调用继承原始 `userId`，显式绑定和 Agent 所有权都不能形成身份提权。
