# Group 模式

本文是 Group 的产品语义、成员来源、协作方式、用户交互和生命周期的唯一权威来源。

## 定义

Group 是工作网中多个完整 Agent 围绕当前目标形成的逻辑协作关系：

```text
Enterprise Agent Network
        ↓ 选择 / 搜索 / 邀请
GroupSession
├── Members
├── Messages
├── AgentInvocations
├── optional SharedTasks
├── Shared Artifacts / Facts
└── GroupRuns
```

Group 不是角色模板、流程实例、Task DAG、常驻进程集合或中央 Supervisor 管理的团队。Agent 是逻辑节点，Membership 构成临时协作子网，真实消息和调用形成交互边。

GroupSession、Membership 和共享记录可以长期存在而没有任何 Runner。用户消息、定向消息或 AgentInvocation 需要处理时，平台才为目标成员创建或恢复 WorkItem 并调度 Runner。

## 成员从哪里来

所有成员必须是工作网中可发现或可直接寻址的真实 Agent。第一阶段工作网直接查询 MyClaw 数据库中的 AgentCard 投影；Group 不在内部创建 Agent 副本，也不根据“产品经理”“开发”“测试”等角色名称临时生成 Prompt Agent。

成员进入 Group 有两条路径：

1. 用户从工作网直接选择 Agent；
2. 当前 Agent 搜索 AgentCard，并提出或执行邀请。

是否允许自动邀请由 Collaboration Policy 决定。`group_enabled` 只表示可以成为候选；发现不等于授权，加入仍需检查原始用户身份、visibility、发布版本、Agent 可用性、共享范围、权限、预算和企业策略。

## 为什么不需要模板

Group 成立只需要：

- 当前协作目标或用户消息；
- 真实成员集合；
- 必要的共享上下文；
- 当前权限、预算和可见性策略。

产品/开发/测试模板会把高频场景误写成 Group 本体，容易导致：

- 角色槽位取代真实 Agent 能力；
- 初始角色关系变成固定调用拓扑；
- UAT 场景限制污染终局动态社会；
- 平台重新掌握“谁应该怎样协作”的决定权。

用户可以保存成员收藏、场景提示或 UAT Fixture 方便复用，但这些属于产品快捷方式或测试资产，不是 Group 架构依赖。

## 建立 Group

### 用户直接建组

用户选择两个或多个工作网 Agent，输入目标并确认共享范围。平台创建 GroupSession、初始 Membership 和第一条用户消息，随后由成员自主响应。

### Agent 提议建组

当前 Agent 判断直接委派不足时，可以说明：

- 为什么需要多 Agent 直接协作；
- 建议邀请哪些 Agent；
- 每个候选的 AgentCard 摘要；
- 准备共享哪些上下文；
- 预计成本、权限和风险。

第一阶段由用户确认后创建 Group。未来命中预授权 Collaboration Policy 时可以自动建立，但仍写入相同成员和授权事件。

建立 Group 不要求模型输出完整 Formation JSON，不要求预置初始 WorkItem，也不要求用户编辑协作边。

## 核心对象

### GroupSession

长期协作空间，保存成员事件、消息、共享任务、Artifact 和多个 GroupRun：

```text
open | closed | archived
```

`busy / idle` 从 GroupRun 关联的非终态 Run 和 Invocation 派生，不作为反复切换的领域状态。

### GroupMembership

成员关系至少记录：

```text
group_session_id
agent_id
status: invited | joined | declined | cancelled | left | removed
invited_by
reason
context_scope
authorization_source
joined_at / ended_at
```

AgentVersion 不必永久写死在 Membership；每个 GroupRun 或 AgentInvocation 保存实际版本快照，兼顾 Agent 成长、恢复和历史审计。

### GroupRun

一次用户目标、后续追问或定向工作形成一个 GroupRun。每个 GroupRun 必须与一个 Run 一对一关联，Run 是 lifecycle、等待原因、终态和结果的唯一权威；GroupRun 不保存第二份 `status/outcome`，因此不存在双写同步问题。

GroupRun 只保存协作聚合事实：`group_session_id`、唯一 `run_id`、本轮负责向用户汇总的 `reporter_agent_id`、成员/AgentVersion 快照、共享范围、预算与策略，以及汇总 epoch。API 和 UI 中的 GroupRun 状态全部从关联 Run 派生。

第一阶段 `membership_policy=fixed_for_run`，本轮成员不变化；长期模式可以允许成员事件在后续 Turn 或新 Run 生效。

### GroupMessage

消息至少支持：

```text
private | direct | group | user
```

只有接收者因定向消息被唤醒。Group 消息对成员可查询，但不会把全文自动注入每个 Agent 的 Prompt。

### SharedTask（可选）

成员可以按需创建、认领、更新和完成共享任务：

```text
title
description
status: pending | in_progress | completed | blocked | cancelled
assignee?
depends_on[]?
result_refs[]
```

SharedTask 是 Agent 社会的协作白板，不是平台工作流：

- 不要求 Group 启动时预先创建；
- 不自动调用 assignee；
- 不限制成员在任务外采取必要行动；
- 依赖只影响认领提示和 UI，不代表授权；
- 成员可以自行调整，用户也可以直接干预。

### AgentInvocation

成员可以向其他成员发送有边界的工作请求。目标 Agent 可以接受、追问、反提议、拒绝、失败或返回结果。调用与普通消息分开，因为它具有等待、预算、版本、权限和恢复语义。

它和 Chat/Plan 委派使用同一 AgentInvocation、调度器和 Agent Loop。Group 只增加 `group_session_id`、Membership 校验、共享上下文范围、消息 visibility 和结果路由。

## 默认对话锚点与汇总责任，不是 Supervisor

创建 Group 的入口 Agent 记录为 `initiator_agent_id`。当用户没有 @具体成员时，它默认先处理消息。每个 GroupRun 另固定 `reporter_agent_id`；默认取本轮入口/发起 Agent，也可以由用户选择其他可用成员。Reporter 只承担“结构空闲时生成用户可见汇总”的责任，不获得额外 Tool、数据或管理权限。

它没有额外权力：

- 不能修改其他成员 AgentVersion 或装配；
- 不能替其他成员调用 Tool；
- 不能删除成员消息、失败或异议；
- 不能强制成员按某一流程工作；
- 不能批准其他成员的权限请求；
- 不能阻止用户直接联系任一成员。

某次后续 GroupRun 可以由不同成员作为用户消息锚点，不需要改变 Group 的所有权。

## 成员如何工作

每个成员运行同一个 Agent Loop，并拥有自己的：

- AgentVersion 与 Instructions；
- Context 和私有 Memory；
- WorkingPlan；
- Tool、Skill、Sandbox 和权限；
- WorkItem、预算消耗和按需调度的短生命周期 Runner；
- 专业判断与最终消息。

成员通过消息、AgentInvocation、SharedTask、Artifact 和共享事实协作。平台不预先规定谁先调用谁、必须返工几次或谁有最终专业权威。WorkItem 在有工作或等待时持久存在；Runner 只在成员实际推进工作时短暂存在。

## 用户与 Group 交互

用户可以：

- 向整个 Group 发送目标；
- @任一成员直接交谈或分配新工作；
- 查看每个成员的 AgentCard、版本、状态和计划；
- 查看真实 Interaction Graph；
- 回答成员 Clarification；
- 批准精确 Tool 或成员加入请求；
- 修改、创建或关闭 SharedTask；
- 要求成员相互复核、讨论或保留不同意见；
- 暂停、取消当前 GroupRun 或关闭 Group；
- 离开页面后让云端继续工作。

返回父 Chat 只改变 UI，不取消 Group。

## 共享上下文

入口 Agent 的完整历史不会自动共享。新 Group 只注入：

- 用户当前目标与明确共享的背景；
- 选定 Message、文件、Artifact 和事实引用；
- 当前成员、权限、预算和可见性摘要；
- 由用户或发起 Agent编写的高信号 Bootstrap。

新增成员只获得其加入时允许的 Bootstrap 和可查询共享记录。成员的 Instructions、私有 Memory、完整 Prompt、凭证、未发布私有文件和 Chain-of-Thought 永不共享。

## 动态成员

成员发现能力缺口时，可以搜索工作网并提交邀请。邀请说明候选、原因、共享上下文、预计预算和风险。

Collaboration Policy 可以逐步开放：

1. `manual_only`：所有成员变化由用户批准；
2. `trusted_candidates`：预授权候选和预算内自动加入；
3. `network_policy`：在组织、数据、成本和风险规则内自主发现和组队；
4. `federated`：跨组织 Agent 在联邦身份和授权下协作。

第一阶段使用 `fixed_for_run`：用户从工作网选择产品、开发、测试三个 Agent 后，本轮不加入第四个成员。模型仍可以搜索和提出候选，平台形成可审计拒绝或等待，不需要创建新的 Charter 或 GroupSession 才能表达未来能力。

## 计划和协调

每个成员可以维护自己的 WorkingPlan。Group 需要全体协调时，可以使用 SharedTask 或 Group Message 形成共同状态。

平台不要求所有成员共享同一计划，也不把 SharedTask 变成自动编排器。一个严谨测试 Agent 可以在共享任务之外追加验证；一个耐心开发 Agent 可以多次修复；一个产品 Agent 可以质疑“测试通过”是否覆盖真实用户目标。

## 停止、等待和交付

成员返回普通最终消息时，只结束自己的当前 Turn 或 AgentInvocation。GroupRun 是否仍在工作由机器状态判断：

- 是否存在 active Runner；
- 是否存在 running/waiting Invocation；
- 是否等待用户或 Approval；
- 是否存在明确已认领且正在进行的 SharedTask；
- 是否被取消或达到硬预算。

Group 的自然结束必须由 `GroupRunCoordinator` 根据持久化机器事实推进，不能依赖某个进程碰巧仍在线：

1. 任何可能减少 pending work 的提交，例如 Runner、Invocation、Approval、Clarification 或已认领 SharedTask 结算，都会写入一次幂等的 `evaluate_group_quiescence` Outbox；
2. Coordinator 锁定 GroupRun 与关联 Run，确认没有 active Runner、非终态 Invocation、待处理人类动作或明确已认领且进行中的 SharedTask；
3. 如果不存在 queued/running SummaryRequest，Coordinator 在锁内递增 `summary_epoch`，写 `group.summary_requested`，并为新 epoch 创建 Reporter 的 WorkItem、queued Runner 与 Outbox；唯一键 `(group_run_id, summary_epoch)` 防止重复唤醒；
4. Reporter 基于可见的共享事实、失败和分歧生成普通最终消息。若本 Turn 又创建 Tool/Invocation/等待，Reporter WorkItem 按普通规则等待并在结果回来后于同一 summary epoch 恢复；
5. 若 Reporter 交付前出现与该汇总无因果关系的新用户/成员工作，当前 SummaryRequest 标记 `superseded`；新工作收敛后才递增 epoch 并重新唤醒 Reporter，防止用过期事实完成 Run；
6. Reporter 消息提交且再次确认没有 pending work 时，原子更新 `delivered_summary_epoch` 并把唯一 Run 置为 `completed`。GroupRun 的完成展示直接读取该 Run；
7. 用户取消或硬预算耗尽时，Runtime 可以直接以机器生成的状态摘要结算 cancelled/failed，不要求模型再汇总。Reporter 在汇总前不可用或持续失败时，当前 SummaryRequest/Reporter WorkItem 终态失败，Run 进入 `waiting_user`，让用户选择另一个现有 available 成员。回答仅更新 Reporter、结算 Clarification 并重新评估空闲；平台为下一个 epoch 创建新 SummaryRequest/WorkItem，不复活旧 Runner，也不得静默赋予其他 Agent Supervisor 身份。

用户也可以在 Run 仍 active/waiting 时要求任一成员先交付阶段性消息；阶段性消息不改变 Run 的 pending 事实。平台不要求 `OutcomeClaim`，也不强制独立 Evaluator。

如果业务有确定性 Gate，例如程序必须通过九例，平台从真实 ToolAttempt 和 Artifact 计算 verified 状态。缺失验证不会阻止成员报告现状，但不能显示为已验证成功。

## 分歧和失败

成员可以直接记录不同意见、反证和风险。汇总消息必须引用或说明影响结论的重要分歧，不能把它们静默平均掉。

失败、拒绝、超时、预算耗尽、结果未知和成员退出都是正常终态事实。它们应返回给相关 Agent 和用户，不能让 Group 永久 waiting，也不能因一个成员失败自动抹掉其他成果。

## 版本与恢复

GroupRunSnapshot 固定每个参与成员本轮实际使用的 AgentVersion。新发布版本只影响未来 Run 或未来 Invocation 的解析策略，不能热替换已经执行中的 Runner。

等待期间 Runtime 保存 Membership、消息游标、WorkingPlan、SharedTask、Invocation 结果、权限和预算。恢复创建新 Runner，旧 lease 的迟到写入被 fencing 拒绝。

终态 GroupRun 不复活。后续消息创建新 Run 并引用历史。关闭 Group 不回滚已经发生的副作用。

## 第一期固定开发组 UAT

第一阶段路径是：

1. 产品、开发、测试 Agent 各自发布到工作网；
2. 用户从工作网明确选择这三个 Agent；
3. 平台创建 GroupSession 和 `fixed_for_run` Membership 快照；
4. 用户发送容量风险计算器目标；
5. 成员自主讨论、创建或认领工作、调用 Tool 和互相复核；
6. 用户可以直接回答任一成员或要求反向复核；
7. 实际消息和 AgentInvocation 形成 Interaction Graph；
8. 程序、九例测试和 Artifact 由真实环境验证；
9. Group 自然空闲后由本轮 Reporter 的幂等唤醒完成最终汇总和交付。

验收不要求 Formation Proposal、角色模板、固定协作边、Charter Revision 或 LLM Outcome Evaluator。

## 禁止设计

- 在 Group 内重新创建或复制 Agent；
- 通过角色名称自动赋权；
- 强制 Group 模板和角色槽位；
- 要求模型一次性生成完整 Formation JSON；
- 把初始建议边当作调用授权或执行流程；
- 让入口 Agent 成为永久 Supervisor；
- 平台根据 SharedTask 自动调度业务；
- 为了动态成员引入重型 Charter 文档；
- 让用户只能通过入口 Agent 与其他成员交流；
- 用通用 `OutcomeClaim + Evaluator` 决定所有 Group 是否结束；
- 把一期固定三个 Agent 写成终局规则。
