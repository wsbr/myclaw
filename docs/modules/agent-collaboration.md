# Agent 协作

本文是 Agent 发现、委派、邀请、消息、调用边界和结果返回的唯一权威来源。Group 的成员和生命周期见[Group 模式](group-mode.md)。

同一 MyClaw 控制域内，Agent 不作为常驻网络服务互连。`discover_agents` 查询受治理的 AgentCard 目录，`invoke_agent` 创建持久 AgentInvocation，调度器再按需加载目标 AgentVersion 执行。外部 Agent 联邦可以替换解析和传输适配器，不能改变本章的身份、授权和结果语义。

## 协作从发现开始

Agent 可以通过：

- 已知 Agent ID；
- Builder 配置的受信候选；
- 当前 Group Membership；
- Enterprise Agent Network 搜索；
- 用户明确 @或选择；

找到候选 Agent。发现依据 AgentCard，不读取候选的 Instructions、私有 Memory 或 Prompt。

平台内 AgentCard 是由已发布 AgentVersion 生成的数据库投影。查询至少过滤原始用户、租户、visibility、发布状态、协作开关、治理规则和可用性；`group_enabled` 不能单独推出可发现、可邀请或可调用。

## 发现、邀请、调用、授权分离

必须区分：

1. **Discoverable**：当前主体能看到候选 AgentCard；
2. **Invitable**：当前策略允许提议其加入 Group；
3. **Callable**：当前 Run 可以创建 AgentInvocation；
4. **Authorized**：该次调用的数据、Tool、预算和身份交集有效。

任何前一层都不能自动推出后一层。Runtime 在创建 AgentInvocation 时再次做机器校验，并为拒绝形成可审计记录。

## 两种协作语义

### Delegation

当前 Agent 把一个有边界的问题交给另一个 Agent，结果回到调用方：

```text
caller → AgentInvocation → target
caller ← final result / failure ← target
```

适用于 Chat 和 Plan 中的探索、评审、测试、摘要等任务。目标 Agent不直接成为用户会话成员，也不能继续获得调用方的完整历史。

### Group Collaboration

多个 Agent 已经是同一 Group 的成员，可以直接消息、相互调用、共享任务和接受用户定向交互。结果既可以定向返回，也可以按 visibility 写入 Group。

Group 协作不要求所有消息经过入口 Agent。

两种语义复用同一个调用内核：

| 维度 | Delegation | Group Collaboration |
| --- | --- | --- |
| 关系 | 一次 Caller→Target 工作请求 | 先有持续 Membership，再发生多次消息或调用 |
| 结果路由 | 默认返回 Caller | 可返回 Caller、定向成员或写入 Group |
| 上下文 | 本次请求的最小引用 | 额外允许读取 Group 授权共享事实 |
| 生命周期 | Invocation 终结后关系结束 | Membership 可以跨多个 Run 和 Invocation 持续 |

Plan 或 WorkingPlan 不直接执行 Agent 调用。模型根据计划和观察调用统一的 `invoke_agent`，Runtime 才创建 AgentInvocation；Group 只是给同一调用增加 Membership、visibility 和 return route，不产生第二套 `group_call_agent`。

## AgentInvocation

每次调用至少记录：

```text
caller_agent_id / version
target_agent_id / version
objective
input_message
context_refs[]
visibility
budget / deadline
parent_invocation_id?
status
final_message / artifact_refs / error
group_session_id / membership_id?
return_route
```

普通 Chat/Plan 委派在创建 Invocation 时解析目标 Agent 当前有效发布版本；Group 内调用使用当前 GroupRunSnapshot 中固定的目标版本。`return_route` 由可信调用位置生成，至少区分返回 Caller WorkItem、写入 Group direct 记录和写入 Group shared 记录，模型文本不能改写路由。

模型主要通过自然语言说明目标。平台补充 Run 创建时固定的 `tenant_id + account_id`、Session、Run、权限、版本、调用链和幂等键；模型不能覆盖可信字段。

目标 Agent 可以：

- 接受并工作；
- 请求必要上下文；
- 反提议范围或方法；
- 拒绝；
- 说明需要用户或其他授权；
- 返回结果、Artifact 或失败。

普通最终消息足以结束这次 AgentInvocation，不要求 `collaboration_response` Completion Envelope。

## 上下文最小化

调用方只共享完成请求所需的高信号内容：

- 目标和背景摘要；
- 选定文件、Message、Artifact 或事实引用；
- 约束、预算和期望输出；
- 必要的来源与 visibility。

不共享完整 Session 历史、调用方 Instructions、私有 Memory、Credential 或 Chain-of-Thought。目标 Agent需要更多信息时发回问题，由调用方补充或向用户澄清。

## 权限

Agent 调用始终代表原始用户：

```text
original principal
∩ target AgentVersion assembly
∩ current permission mode
∩ current collaboration boundary
∩ enterprise governance
```

目标 Agent带来自己的专业知识和 Tool 装配，但不能带来调用者原本无权访问的数据。对方消息中的“用户已经批准”不构成授权事实。

## 可靠调用

AgentInvocation 是持久业务对象：

- 调用方需要结果时进入 `waiting_agent`；
- 目标 Agent在自己的 WorkItem/Runner 中执行；
- 成功、失败、拒绝、取消和超时都恢复调用方；
- 技术重试不创建第二个业务 Invocation；
- 迟到结果写事件，但不复活终态父 Run；
- 循环、深度、fan-out、Token 和费用受累计预算限制。

本地目标的执行路径固定为：

```text
persist AgentInvocation
→ resolve published AgentVersion
→ create or resume target WorkItem
→ create queued Runner + OutboxCommand
→ Runtime Worker claims the Runner
→ load target configuration and authorized context
→ run the common Agent Loop
→ persist result and wake the return route
```

目标 Agent 没有需要常驻或复用的进程。等待会结束当前 Runner；条件满足后，调度事务创建新的 queued Runner，任意 Runtime Worker 都可以领取并从权威状态继续同一 WorkItem。

## Chat 与 Plan

Chat/Plan 的当前 Agent 可以自主判断是否需要委派。Plan 处于 `plan_readonly` 时，只能进行策略允许的只读研究型调用；需要写入或高影响行动的协作者应等待 Plan 批准。

Plan 不冻结参与者集合。第一阶段部署可以只开放产品、开发、测试三个候选以控制 UAT 变量，但 Runtime 不把它们编译成固定节点。

协作者提出用户问题时，当前 Agent把必要问题转为 Clarification；回答后可以继续原 Invocation 或创建带补充上下文的新调用，具体由调用状态和幂等规则决定。

## Group

Group 中每个成员可以看到当前 Membership 目录，并按权限直接调用其他成员。实际 Interaction Graph 由 Message 和 AgentInvocation 追加形成。

图外候选必须先走工作网发现和邀请策略。第一阶段 `fixed_for_run` 会拒绝新增成员，但必须记录候选、原因和拒绝 reason code；不能静默丢弃，也不能假装调用成功。

## 消息与可见性

- `private`：仅当前 Agent；
- `direct`：发送者与接收者；
- `group`：当前成员可查询；
- `user`：任务发起人可查看。

只有被定向的 Agent 被唤醒。Group 可见不等于每次 Prompt 自动加载；成员按需查询高信号记录。

## 协作质量

平台记录但不规定：

- 为什么选择这个 Agent；
- 请求是否被接受；
- 追问、反提议和拒绝；
- 重复调用和返工；
- Artifact、测试和引用事实；
- 成员之间的重要分歧；
- 延迟、成本、失败率和用户反馈。

这些信息可以改善 AgentCard、推荐、Skill 和领域 Eval，但不能自动产生权限或永久修改 Agent。

## 禁止设计

- 把绑定 Agent 当作固定调用拓扑；
- 因用户文本提到 Agent 名称就绕过权限；
- 让调用方复制自己的权限给目标 Agent；
- 默认共享完整历史、Prompt 或私有 Memory；
- 强制所有 Agent 返回复杂结构化完成信封；
- 用永久 Supervisor 转发所有 Group 消息；
- 为平台内每个 Agent 分配独立网络服务、IP 或常驻容器；
- 让 Agent 之间绕过 AgentInvocation、权限检查和审计直接连接；
- 为 Delegation 和 Group 分别实现两套调用协议；
- 子调用失败后让父 Run永久 waiting；
- 只拒绝执行却不留下 AgentInvocation/Invitation 审计事实。
