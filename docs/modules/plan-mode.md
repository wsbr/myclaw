# Plan 模式

本文是 Plan 的产品语义、冻结任务图、中心化协作和生命周期的唯一权威来源。

## 定义

Plan 是面向长任务的中心化执行模式。当前与用户交互并创建计划的 Agent 是本次 `Plan Owner`；它不是平台内置 Orchestrator，也不是永久 Agent 类型。

Plan 使用 Work Item Graph 表达任务拆解与流转：

```text
Plan Proposal
├── Owner Agent
├── Declared Agent Set
└── Work Item Graph
    ├── Work Item Nodes
    └── Dependency Edges
```

图节点是要完成的工作，图边是工作依赖或流转关系。它与 [Group 模式](group-mode.md) 中“节点代表参与 Agent”的图具有不同语义。

## 可用性与入口

所有可交互 Agent 固有支持 Chat 和 Plan，不需要 `plan_enabled`。用户在任意 Agent 侧都可以显式选择 Chat 或 Plan。

Chat Agent 可以建议用户改用 Plan，但不能自行切换执行模式。用户选择 Plan 后，Owner 生成 Proposal；平台展示给用户，用户确认或修改后才能执行。Plan 不提供自动执行策略。

一次已开始的 PlanExecution 不能切换为 Chat 或 Group，也不能嵌套 Group。需要更换模式时，当前执行必须先结算或取消，再创建新的执行对象。

## Plan Owner

Plan Owner 固定为创建本次 Plan 时用户正在交互的 Agent。用户离线、页面切换或 Runner 重建不会改变 Owner。

Owner 负责：

- 理解用户目标并生成 Work Item Graph；
- 统一与用户澄清；
- 决定哪些节点自己执行，哪些分派给子 Agent；
- 通过 `invoke_agent` 调用计划中声明的 Agent；
- 汇总子 Agent 的定向结果；
- 判断节点和整体目标的语义完成；
- 向原 UserSession 交付最终结果。

平台负责保存、校验和冻结图，调度可执行状态，持久化等待、取消、失败和 ExecutionEvent。平台不替 Owner 做领域判断。

## 严格星形协作

Plan 采用严格星形拓扑：

```text
用户
 ↕
Plan Owner
 ├── invoke_agent → 子 Agent A
 ├── invoke_agent → 子 Agent B
 └── invoke_agent → 子 Agent C
```

规则如下：

- 只有 Owner 可以向子 Agent 发起 `invoke_agent`；
- 子 Agent 不能调用其他子 Agent，也不能另起 Plan 或 Group；
- 子 Agent 结果只返回 Owner；
- 子 Agent 需要其他领域协助时返回 `needs_assistance`；
- 子 Agent 需要用户信息时返回 `needs_clarification` 和建议问题；
- 只有 Owner 直接与用户沟通，再把必要答案通过新 AgentInvocation 传给子 Agent；
- Owner 可以在同一 Runner 中并行调用多个已声明 Agent；
- 平台不会根据图自动隐式调用子 Agent。

## 用户确认与图冻结

用户确认时同时确认：

- 目标和交付物；
- Work Item 定义；
- 依赖边；
- 每个节点的执行 Agent；
- 本次可参与的 Agent 集合。

确认事务为 Owner 和所有执行 Agent 解析当前已发布 `agent_version_id`。确认后的 Plan Definition 和参与 Agent 版本完全不可变。执行过程中只允许更新运行状态，不允许：

- 新增、删除或改写 Work Item；
- 修改依赖边；
- 修改节点执行 Agent；
- 修改验收条件或交付目标；
- 添加未在计划中声明的 Agent；
- 修改 Plan Owner。

执行期间发布某个参与 Agent 的新版本，不替换本次 Plan 固定的 AgentVersion。已经开始的 Plan 可以使用固定版本继续结算；平台级 Tool 或 Model 被紧急停用时仍按当前治理状态明确失败。新 Plan 才重新解析参与 Agent 最新发布版本。

`invoke_agent` 的目标必须同时满足：

```text
目标 Agent ∈ Owner 的直接可发现范围
目标 Agent ∈ 已确认 Plan 的 declared_agent_ids
```

Tool 重试、Runner 续跑、Work Item 状态变化以及 Agent 在节点内部改用另一已装配 Tool，都不属于修改计划。

## 计划失效与后继 Plan

执行结果推翻初始假设时，不修改运行中的图。当前 Plan 停止继续调度，以已有成果结算为部分成功或失败，Owner 可以基于已有成果生成新的 Plan Proposal：

```text
Plan A：completed + partially_succeeded / failed
└── 已完成结果与 Artifact

Plan B Proposal
├── predecessor_plan_id = Plan A
└── 引用仍有效的成果
```

Plan B 需要用户重新确认，是独立 PlanExecution。旧 Plan 的图、状态、副作用和执行轨迹不被覆盖，也不承诺回滚。

## 执行与完成

平台只调度依赖已满足的 Work Item。Owner 自己执行节点或显式调用对应子 Agent；所有等待采用 [Runtime 执行](runtime-execution.md) 定义的可恢复 Runner 模型。

并行 AgentInvocation 采用 all-settled：平台等待本轮所有调用进入终态，再恢复 Owner；单个失败不会自动取消其他调用。Owner 根据成功、失败、取消或拒绝结果决定语义结论。

Work Item 成功、失败、超时、取消、放弃或依赖阻塞都属于明确结果。失败节点不让 Plan 永久悬挂；Owner 可以在确认图范围内重试原 Work Item、跳过无法继续的节点或提前收敛，但不能修改图或增加 Agent。

PlanExecution 使用两个维度：

```text
lifecycle: running | waiting | settling | completed | cancelled
outcome: succeeded | partially_succeeded | failed
```

当工作均形成终态结果，或用户 / Owner 明确要求收敛时，Owner 向原 UserSession 提交统一汇报，列明成功、失败、跳过、取消、未完成内容和已有 Artifact。

未完成工作可以阻止 `succeeded`，但不能阻止 Plan 进入终态。核心目标只完成一部分时为 `completed + partially_succeeded`；目标未达成但失败已经明确汇报时为 `completed + failed`。用户主动取消则为 `cancelled`。
