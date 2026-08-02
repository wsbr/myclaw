# 上下文与记忆

本文是上下文策略、历史窗口、记忆策略和记忆作用域的唯一权威来源。

## 四类策略 Registry

平台代码实现并注册四种彼此独立的策略类型：

```text
Context Assembly Policy
History Window Policy
Memory Write Policy
Memory Recall Policy
```

每个 Policy Definition 至少包含：

```text
key
type
name
description
implementation_ref
version
enabled
```

内部可以维护 Token 预算、TTL、排序阈值、存储后端等实现参数，但这些是平台策略的一部分，不是 Agent 自定义配置。

## 各策略职责

### Context Assembly Policy

决定如何组织当前执行所需的业务上下文、Session 状态和 Runtime 元数据，不负责选择历史消息或长期记忆。

### History Window Policy

决定从当前 UserSession、Plan 对话或 GroupSession 选取、裁剪或总结哪些历史消息。

### Memory Write Policy

决定 Runner 完成时是否提取记忆、写入何种作用域以及保存哪些来源引用。

### Memory Recall Policy

决定当前输入下召回哪些记忆、如何排序和如何形成 Prompt 段落。

Agent 必须为四个维度各选择一个已启用策略 key。平台可以提供显式的 `none` 策略，但不能以空值形成不明确行为。

## 第一阶段内置策略

### Context Assembly

第一阶段只注册：

```text
context.standard
```

它按 [Runtime 执行](runtime-execution.md) 定义的固定 Prompt 区段组织当前业务上下文和 Runtime 元数据。第一阶段不提供按 Agent 自定义 Prompt 区段顺序的策略。

### History Window

第一阶段注册：

```text
history.recent
history.summary_recent
```

- `history.recent`：在当前 Agent 的直接可见历史中保留最近窗口，超过平台 Token 预算时截断更早内容。
- `history.summary_recent`：保留当前可见历史的摘要和最近窗口；摘要由平台异步更新。

Token 预算、消息数量和摘要触发阈值属于平台注册配置，不在 Agent 页面填写。摘要失败时可以退化为同一可见范围内的最近窗口，但不能切换 Agent 模型或扩大可见范围。

历史摘要是 Session 历史压缩结果，不是长期记忆。

### Memory Write

第一阶段注册：

```text
memory_write.none
memory_write.user_agent_facts
memory_write.user_facts
```

- `memory_write.none`：不写长期记忆。
- `memory_write.user_agent_facts`：从用户明确表达或确认的信息中提取稳定事实，写入 `user_agent` 作用域。
- `memory_write.user_facts`：只提取适合跨 Agent 使用的稳定用户事实，写入 `user` 作用域。

模型推测、临时计算、内部分析过程和未经用户确认的 Tool 结果不能直接写为长期记忆。记忆写入异步执行；失败记录 ExecutionEvent，但不把已经完成的原 Run 改为失败。

### Memory Recall

第一阶段注册：

```text
memory_recall.none
memory_recall.user_agent
memory_recall.user_combined
```

- `memory_recall.none`：不召回。
- `memory_recall.user_agent`：只召回同 `userId + agent_id` 的记忆。
- `memory_recall.user_combined`：召回当前 Agent 专属的 `user_agent` 记忆和同用户的 `user` 记忆，优先专属记忆。

召回结果必须记录使用过的 `memory_id` 到 Runner 的可解释快照或 Trace。策略可以排序和裁剪召回结果，但不能越过作用域查询。

## 记忆作用域

### `user_agent`

同一用户与同一 Agent 跨 Session 共享。适合该用户与某个领域 Agent 的偏好、口径和持续事项。

### `user`

同一用户跨 Agent 共享。适合多身份、多 Agent 协作时需要复用的用户级事实。召回仍由当前 Agent 的 Memory Recall Policy 决定。

### `agent`

同一 Agent 跨用户共享。当前只保留数据模型与策略接口，不在现阶段启用写入和召回，用于后期受审核的 Agent 进化。

所有记忆必须记录作用域键、来源 Session/Runner、创建策略和可审计元数据，避免跨作用域误召回。

第一阶段 Memory 至少记录：

```text
memory_id
userId
agent_id?
scope
content
source_session_id
source_message_ids[]
created_by_runner_id
policy_key
status
created_at
updated_at
```

相同或冲突事实需要受控更新、合并或停用，不能无界重复追加。用户可以查看、修改和删除自己的记忆；删除或停用后不得继续召回。

## Group 中的上下文

Group 只有一个共享 GroupSession，不为每个节点建立独立聊天 Session。每个节点以 `group_id + agent_id` 为命名空间，先确定当前 Agent 的直接可见范围，再由自己的策略在该范围内构建上下文。

直接可见范围包括：

- Group 目标、已确认 Agent 图和整体状态；
- 当前 Agent 自己发送和接收的消息；
- 用户定向发给当前 Agent 的消息；
- 当前 Agent 发起或收到的 Clarification；
- 分配给当前 Agent 的 Work Item；
- 当前 Agent 参与的 AgentInvocation 请求与结果；
- 明确发送给当前 Agent 的 Artifact；
- 当前 Agent 自己的 ToolInvocation 和执行结果。

Context、History 和 Memory Policy 只能在上述可见范围内裁剪、召回和组装，不能借助策略读取其他 Agent 的对话或工作。

其他 Agent 之间的消息、Work Item、调用、结果和发布到 Group 的共享 Artifact 属于 Group 共享记录。它们不会自动进入当前 Agent Prompt，必须通过显式装配的 `search_group_history` Platform Tool 获取。查询结果作为普通 ToolInvocation 结果进入当前 Runner。

GroupTranscriptDocument 是共享 Message 与 ExecutionEvent 的派生只读文档或索引，不是上下文策略的隐式数据源。Group 不自动添加查询 Tool；启用 Group 时管理面可以提示能力缺失，但不能替 Agent 装配。

`history.summary_recent` 的 Group 摘要也必须按 `group_id + agent_id` 分别维护，不能生成一份全群摘要后自动注入所有 Agent。通过 `search_group_history` 得到的结果只进入当前 Runner 的可见上下文，不自动写入长期记忆。

共享 GroupSession 不等于共享私有配置。其他节点不能读取本 Agent 的 Instructions、私有记忆、Runner Prompt、模型内部推理或敏感凭证。

## 后期 Agent 进化

进化不是当前执行能力。后期候选流程为：

1. 从 Session/Runner 快照提取可审计的事实记忆；
2. 跨用户聚合过程性经验；
3. 生成 Skill Candidate，而不是直接修改 Agent；
4. 管理员审核内容、来源和风险；
5. 通过后发布为新 Skill 或 Skill 新版本。

系统不得自动修改 Agent Instructions、装配项或已发布 Skill。
