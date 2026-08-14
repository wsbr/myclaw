# Group 模式

本文是 Group 的产品语义、成员来源、协作方式、用户交互和生命周期的唯一权威来源。

## 定义

Group 是工作网中多个完整 Agent 围绕当前目标形成的逻辑协作关系：

```text
Enterprise Agent Network
        ↓ Chat 模型按用户诉求语义选员
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

当前产品路径只有一个默认入口：用户先进行正常 Chat；root Runner 按当前身份边界发现所有 `group_enabled`、已发布、可见、可用、可调用且 `group_allowed` 的 Agent 作为完整候选目录，由模型按用户诉求选择必要协作成员并保留 Chat 会话锚点。Chat 的用户消息和首轮回答提交成功后，可靠 Outbox 才自动形成或复用 GroupSession、创建新的 GroupRun。该过程不回滚已经成功的 Chat。

旧版 `manual_selected` Group 及创建 API 仅为历史记录和兼容性保留，不再是 Web 主入口。后续每次 Chat 由模型按当次诉求重新选择未来成员；成员变化写入 joined/removed 事件，本次 GroupRun 仍冻结当时的成员与 AgentVersion 快照。

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

### Chat 后自动涌现

正常 Chat 的 root Runner 先冻结治理后的 Group 候选目录，仅把每个 Agent 的 `agent_id/name/description` 作为数据放入提示词。当前 Chat Agent 在自身满足候选治理时固定保留为用户的会话锚点；模型根据本次用户诉求调用 `select_group_members`，从完整目录中选择围绕该锚点的最小必要协作成员，而不是默认选择全部。Chat Run 完成后，平台异步创建 `emerge_group_after_chat` 控制命令；命令以源 Chat Run 幂等，复核所选 Agent 的实时可见性、可调用性、Group 治理和 published version，再固定本次 GroupRun 的成员快照。

`select_group_members` 与当前 Chat 内的 `invoke_agent` 互斥：一旦选定后续 Group，Chat Agent 不得提前调用同一批成员并重复执行。Group 建立后，入口 Agent 只作为用户的会话锚点，从冻结成员的 `name/description` 语义选择一个最适合的首个主责任人，异步 handoff 主任务并立即向用户确认“项目组已成立、任务已交给谁”；它不等待主责任人在同一 Turn 返回，也不默认把任务 fan-out 给所有成员。

模型没有选出至少两个合适成员、完整候选目录超过部署安全上限，或成员在 Chat 完成前失去资格时，系统写 `group.emergence_skipped`，不从截断目录猜测，也不静默替补其他 Agent。

模型的结构化决定先写 `group.emergence_selected`；第一次形成写 `group.emerged`，同一 Session 的后续 Chat 写 `group.run_emerged`。Web 收到形成事件后切到对应 GroupRun，但刷新或离开页面不影响后台执行。

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

`direct` 是群聊内 human 可见的定向 `@` 消息，只有明确接收者会因新定向消息被唤醒；未参与该对话的成员不会被通知，也不会自动获得正文。`AgentInvocation` 在群聊里会自动投影为“调用方 `@` 目标方”的委托消息和“目标方 `@` 调用方”的交付消息，不需要目标方再重复广播一份。Group 内所有 `invoke_agent` 都是异步 handoff：调用只返回已派发确认，调用方结束当前 Turn；目标结果作为新的定向消息唤醒调用方的新 Turn，而不是伪装成原 Tool Call 的同步 Tool Result。

`group` 不是向所有成员推送的广播，只是群聊的精炼工作留痕。成员只用它记录总结性的进度、重要分歧、风险和结果；它不唤醒任何成员，也不自动注入其他成员的 Prompt。其他成员要了解未参与的定向交付过程或群留痕，必须主动调用 `search_group_history`。用户作为 Group 的 human owner 可以在 Group 消息时间线中查看这些持久化记录。

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

成员可以向其他成员发送有边界的工作请求。目标 Agent 可以接受、追问、反提议、拒绝、失败或返回结果。调用与普通消息分开，因为它具有 handoff、预算、版本、权限和恢复语义。

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

成员通过定向 `@` 消息、AgentInvocation、精炼群留痕、SharedTask、Artifact 和共享事实协作。平台不预先规定谁先调用谁、必须返工几次或谁有最终专业权威。WorkItem 在有工作或等待时持久存在；Runner 只在成员实际推进工作时短暂存在。

每个 Group Turn 都获得冻结成员的 `agent_id/name/description` 数据目录，由当前模型按任务语义决定下一次 handoff，而不是把所有成员拉成一张预制执行图。入口锚点到首个主责任人记为 ownership transfer；主责任人完成自己的部分后，可以把需要核验的子任务异步 handoff 给另一成员。只要当前 WorkItem 还有未整合的下游结果，它就保持 `waiting_agent`，不能向上游误交付为完成；下游定向结果到达后，Runtime 创建新 Runner Turn，当前成员整合后再向自己的调用方交付。

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

GroupSession 使用 `dynamic_future`：每次 Chat 后可以按最新治理结果加入或移除未来成员。每个 GroupRun 始终使用 `fixed_for_run` 快照，本轮不热插拔；模型邀请图外 Agent 时仍形成可审计拒绝或等待。

## 计划和协调

每个成员可以维护自己的 WorkingPlan。Group 需要全体协调时，可以使用 SharedTask 或 Group Message 形成共同状态。

平台不要求所有成员共享同一计划，也不把 SharedTask 变成自动编排器。一个严谨测试 Agent 可以在共享任务之外追加验证；一个耐心开发 Agent 可以多次修复；一个产品 Agent 可以质疑“测试通过”是否覆盖真实用户目标。

## 诊断与循环护栏

Group 允许所有成员异步 handoff，但自由协作不等于无界递归。每个 Run 在硬预算快照中固定 `max_agent_depth` 和 `max_agent_fan_out_per_work_item`；Runtime 在创建新 AgentInvocation 前拦截自调用、因果链回入、同一 WorkItem 的完全重复委托、最大深度和扇出耗尽。模型不能覆盖这些机器边界。

每次拦截都会尽可能写入 terminal `AgentInvocation(status=rejected)`，同时追加一条 `GroupDiagnostic` 和用户可见的 `group.diagnostic_recorded` 事件。诊断只保存 Agent 路径、限制快照、稳定错误码和请求指纹，不保存原始任务正文或思维过程；相同原因按因果指纹幂等。Runtime 控制台通过 `GET /group-runs/{group_run_id}/diagnostics` 展示当前 GroupRun 的决策链。

“护栏已成功收敛一次危险尝试”与“业务工作执行失败”是不同结果：前者保留 blocked 诊断但不单独把最终 Run 标为 `completed_with_errors`；成员失败、取消、超时、目标不可用等真实交付损失仍必须进入错误终态和 Reporter 摘要。

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

## 涌现式 Group UAT

第一阶段路径是：

1. 产品、开发、测试 Agent 各自发布到工作网；
2. 用户先向入口 Agent 发送容量风险计算器目标并得到正常 Chat 回答；
3. Chat 模型根据 Agent 名称与描述选出产品、开发、测试等必要成员；平台复核后创建 `dynamic_future` GroupSession 和 `fixed_for_run` GroupRun 快照；
4. UI 根据 `group.emerged` 自动进入本次 GroupRun；
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
