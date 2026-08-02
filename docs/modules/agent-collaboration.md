# Agent 协作

本文是 Agent 显式绑定、发现目录与 `invoke_agent` 调用边界的唯一权威来源。Group 的图和生命周期见 [Group 模式](group-mode.md)。

## 显式绑定与发现

Agent 的 `bound_agent_ids[]` 定义它在普通运行时可发现的其他 Agent。Runtime 只解析直接绑定的一层，不递归展开被绑定 Agent 的绑定关系。

注入 Prompt 的 Agent Catalog 条目仅包含：

```text
id
name
description
```

`description` 用于说明该 Agent 能承担的任务。Catalog 不包含 Instructions、Tool、Skill、模型、记忆或私有配置。

绑定只是发现范围，不等于调用授权。调用方还必须显式装配 `invoke_agent`，当前 `userId` 也必须有权使用目标 Agent。角色和 `private | enterprise` 访问规则见 [身份与 Agent API](identity-api.md)。

## 唯一交互通道

所有 Agent 间工作请求都通过 Platform Tool `invoke_agent`。平台不提供：

- 隐式子 Agent 调用；
- 基于 Skill 的自动委派；
- Agent 间广播消息；
- 绕过 Tool Dispatcher 的内部直连；
- 因 Group 启用而自动出现的调用能力。

一个调用至少包含目标 Agent、目标说明、传递上下文和关联标识。Runtime 生成并持久化 `AgentInvocation`，目标 Agent 在独立 Runner 中执行，结果定向返回调用方。

## 普通模式授权

Chat 模式采用严格星形协作。当前用户交互 Agent 可以调用直接绑定 Agent；子 Agent 不再调用其他 Agent，也不直接与用户沟通：

```text
callable_agents = agent.bound_agent_ids
```

Runtime 同时校验：

- 调用方 Runner 快照包含 `invoke_agent`；
- 目标 Agent 在调用方 Catalog 中；
- 目标 Agent 启用且对当前租户/用户可访问；
- 输入符合 Tool Schema。

目标 Agent 不继承调用方 Tool、Skill、模型或上下文，只使用自己的装配。

Chat 中每个新的被调用方 Run 使用目标 Agent 当时最新发布版本。Plan 和 Group 则使用用户确认图时固定的目标 AgentVersion，不能在执行过程中切换。

## Plan 模式授权

Plan 同样采用严格星形拓扑，只有 Plan Owner 可以调用子 Agent。有效目标是直接绑定范围与已确认计划声明集合的交集：

```text
callable_agents =
  owner.bound_agent_ids
  ∩ plan.declared_agent_ids
```

子 Agent 的协助或澄清需求必须返回 Owner。Plan 图确认后不能加入新 Agent，详见 [Plan 模式](plan-mode.md)。

参与者的发布、启停和当前用户访问权在图确认时校验并固定为本次 Plan 的执行授权。之后停用 Agent 不破坏已开始 Plan 的结算，但会阻止后续新 Plan 使用它。

## Group 模式授权

Group 中，Prompt 额外注入已确认图的 Group Agent Catalog。可调用目标是初始图中除调用方自身外的参与 Agent，但调用方仍必须拥有 `invoke_agent`。

参与者的发布、启停和当前用户访问权在组图确认时校验并固定为本次 GroupRun 的执行授权。之后停用 Agent 不从当前图移除节点，但会阻止后续 GroupRun 重新选择该 Agent 版本。

调用不会扩群。目标 Agent 已有节点时，新请求追加为该节点的 Work Item，并在节点间创建或更新协作边。请求和结果写入共享 GroupSession，但默认只进入直接调用方和目标 Agent 的可见范围；其他参与 Agent 必须显式查询共享记录。

每次 `invoke_agent` 都要求目标 Agent 返回定向结果。调用方 Work Item 进入 `waiting_agent`，本次 Runner 结束；目标完成后创建新的调用方 Runner 继续。同一 Runner 可以并行调用多个 Agent，平台采用 all-settled 语义，等待全部进入终态后恢复调用方。

## 定向结果与可见性

Chat 和 Plan 中，调用结果只返回中心 Agent。Group 中，请求、结果和关联状态写入共享 GroupSession，直接调用方和目标 Agent 自动可见，其他节点只有显式查询后才能取得。只有直接调用方因结果被唤醒。

Agent 的 Instructions、私有记忆、Runner Prompt、模型内部推理、未公开 Tool 数据和未发布 Workspace 文件始终不共享。
