# V1 Runtime 流程（历史实现参考）

> **历史 V1 实现参考，非目标设计。** Worker 切点、Checkpoint 和 Invocation 恢复仅作为迁移证据；旧冻结 Plan、Formation、Settlement 和 Completion 流程已被[当前综合技术方案](phase-one-agent-native-delivery.md)取代。

本文记录V1 Chat、Plan、Group从请求到交付的时序基线。状态迁移细节见[状态机](state-machines.md)，并发保障见[一致性](consistency-concurrency.md)。

## 1. Runtime 组件

`IdentityResolver` 固定 ExecutionPrincipal；`AssemblyResolver` 解析 AgentVersion/Registry；`ContextService` 执行四类 Policy；`PromptBuilder` 输出有来源的区段；`ModelAdapter` 统一流式响应；`ToolDispatcher` 持久化 Invocation；`AgentInvocationGateway` 校验模式；`RunnerCoordinator` 管理等待/Checkpoint/恢复；`SettlementService` 结算；`EventWriter` 与 `ExecutionQueue` 只在领域事务中使用。

## 2. Runner 启动与装配固定

`runtime.runner` Outbox handler 先按固定切割点分发：claim 命令；短事务锁 Runner/WorkItem 并取得独立 Runner lease/fencing；提交；acknowledge Outbox；然后才在 Outbox claim 之外进入下列长执行。若 ack 失败则不开始模型，Runner lease 交由 control 回收。重复命令遇 running/terminal Runner 只幂等 ack，不创建第二推进者。

取得 Runner lease 后依次执行：

1. 验证 Runner lease、WorkItem execution_epoch、Run 未终态。
2. 读取 WorkItem 固定的 `agent_version_id`。Chat 首 WorkItem 在 Run 创建时解析当前 published；被调用 Chat Agent 在 AgentInvocation 创建时解析；Plan/Group 使用确认表固定值。
3. 读取 SessionSandbox 固定 profile/version并验证 ready。不得使用 AgentVersion 后来选择的 Sandbox 替换。
4. 读取 Model 与 AgentVersion Tool/Policy 关系；任何引用不存在或当前停用，Runner 明确失败。Tool 当前 enabled 会在每次调用再校验。
5. 对 AgentVersion/draft_test_snapshot 已固定的每个精确 SkillVersion 验证共享目录 checksum，写 `runner_skill_snapshot`。缺失同步目录时失败并发起 sync，不读取 logical current，也不用其他版本 fallback。
6. 生成可调用 Agent Catalog：Chat 直接绑定；Plan Owner 为绑定与 declared 集合交集，子 Agent为空；Group 为固定节点集合（排除自身）。
7. 固定 ExecutionPrincipal、model parameter_version、tool implementation/config version、policy parameter version和 Prompt section source manifest。
8. 从最新 safe Checkpoint 或初始输入装载模型循环状态，然后开始上下文组装。

## 3. Prompt 装配

顺序严格为：Platform Runtime Rules；Agent name/description/instructions；Tool Definitions；Skill Catalog；绑定 Agent Catalog；Plan Catalog；Group Catalog；Context Policy；History Policy；Memory Recall；WorkItem/Checkpoint；当前定向输入。

每段记录 `section_key/source_type/source_id/source_version/checksum/token_estimate`。Skill Catalog 仅 `name/description/path URI`；Agent Catalog 仅 `id/name/description`。其他 Agent instructions、私有 Memory、Prompt 与内部推理不可进入。

Token 超预算只能由所选 History/Context Policy 裁剪其负责范围；平台固定规则、当前输入、Tool Schema 和 Agent instructions 超预算时明确 `prompt_too_large`，不能暗换 Policy/Model。

## 4. 模型流式循环

1. ModelAdapter 以 Runner 固定 model/parameter version发起请求，Trace 建 `model_generation` span。
2. 文本 token 经必需但非权威的 Redis Pub/Sub bridge 推给 API，不逐 Token 写 ExecutionEvent；publish 失败仅标记 stream degraded，断线不取消模型。
3. 每个完整模型 turn 保存 Checkpoint：标准化 assistant text、tool calls、provider response id、usage和循环 step。
4. 纯文本语义结果写最终 Message，进入 WorkItem/Run 结算。
5. Tool call 先校验名称必须在 Runner tool snapshot、参数 Schema、模式和身份，再持久化 ToolInvocation；未知或未装配 Tool 以结构化 Tool error 返回模型，绝不补齐。
6. 循环受平台 `max_model_turns/max_tool_calls/max_wall_time` 限制；达到限制明确失败，不切模型。

Provider 流中断：若无完整 turn，技术可重试创建新 Runner或在同 Runner重新请求（仅在未产生外部 Tool 时）；若已提交 Invocation，恢复必须复用 Invocation 结果。完整策略由 Checkpoint step 决定。

## 5. Chat 时序

```text
Client → API → DB transaction → Outbox → Runtime Worker → Model/Tool
       ← 202 Run ids        SSE ← ExecutionEvent      ← final Message
```

1. `POST /sessions/{id}/messages` 验证 userId、Session active、入口 Agent published/可访问；分配 Message 与 Session event sequence。
2. 同事务解析入口 Agent 最新 published AgentVersion，创建 `Run(mode=chat)`、根 WorkItem、queued Runner、`message.created/run.created/runner.created` 和 `runtime.runner` Outbox，返回 202。
3. Runner 按通用启动规则加载已固定的精确 SkillVersion/checksum、装配 Prompt 并流式调用模型。
4. Inline Tool：先落 Invocation/Attempt；无需等待且当前 Runner 可安全继续时，把最终信封加入模型上下文。
5. Confirmation：保存 safe Checkpoint，Runner 结束，WorkItem waiting_user（具体原因 confirmation）；用户批准后同 Invocation 执行，拒绝后以 rejected Tool result 创建新 Runner。
6. Clarification：创建 Clarification 与 Assistant Message，Checkpoint 后结束 Runner；回答创建 User Message并恢复同 WorkItem。
7. `invoke_agent`：校验直接绑定、用户访问、目标启用和星形深度；创建 AgentInvocation、目标独立 WorkItem/Runner。调用方 waiting_agent；子 Agent无 Agent Catalog、不直接澄清用户，其 `needs_clarification/assistance` 是定向结果。目标完成后恢复调用方。
8. Durable Async Tool/`execute`：创建/排队 Attempt，WorkItem waiting_tool/resource；Tool Worker完成后恢复新 Runner。
9. 新 Runner 从 Checkpoint 载入已完成 Tool/Agent 结果；继续使用 WorkItem 固定 AgentVersion 中的精确 SkillVersion/checksum，Model/Policy/Sandbox 也不漂移；Tool 启停、确认、风险和连接治理按当前事实重读。
10. 根 WorkItem 结果写 Assistant Message、Artifact 引用；结算事务直接把 Run 推进到合法终态，不引入 `settling` 状态。Memory Write 异步命令与完成/通知同结算事务写出。

同 Session 下一条用户消息创建全新 Chat Run并再次使用 Agent最新 published版本；前一终态 Run不复活。

Builder受控草稿测试使用独立`draft/test-runs`入口；创建事务把当前 draft revision 复制为不可变 `draft_test_snapshot` 并固定到 WorkItem，标记`execution_kind=draft_test`。只允许私有Chat，不可被其他Agent调用，也不能创建Plan/Group；后续恢复 Runner 继续使用同一 snapshot。它不改变普通Chat“新Run使用最新published版本”的规则。

## 6. Plan 时序

### Proposal 与确认

1. 用户显式选择 Plan并提交目标。API创建 Plan proposal generation WorkItem/Runner；Owner是入口 Agent。
2. Owner模型输出结构化节点、依赖、执行 Agent、目标与验收条件。服务端校验 node key唯一、DAG无环、执行 Agent在直接绑定与访问范围；写 `PlanExecution(draft)` 与关系化图。
3. 用户通过 REST编辑节点、依赖或执行 Agent；每次用 If-Match，服务端重跑全部校验。Agent建议修改同样只产生草稿版本。
4. `confirm` 事务按 Session→Run→Plan 锁定：重新验证 Owner与声明 Agent published/启用/用户可访问；为每个参与者固定最新 published AgentVersion；计算 frozen_hash；Plan definition awaiting→frozen、唯一Run从 waiting/graph_confirmation→running，创建依赖已满足 WorkItem/Runner，写事件与 Outbox。

### 执行

5. 历史 Plan Scheduler 只计算关系表中的“节点已就绪”，不执行领域判断；该旧投影不属于当前稳定 ExecutionEvent。目标方案由 Owner 的 WorkingPlan/AgentInvocation 自然推进，平台不能因文本节点就绪暗中启动目标 Agent。
6. 每个开始执行的计划节点有一个WorkItem。Owner节点使用固定Owner版本；子Agent节点必须由Owner Runner显式调用`invoke_agent`后创建并关联目标WorkItem，平台不得因节点就绪而暗中调用。
7. Owner callable集合严格是固定 declared Agent与其固定绑定的交集。子 Agent Catalog为空，不能调用其他 Agent/Plan/Group；澄清需求返回 Owner。
8. 同一 Owner Runner可并行创建多个 AgentInvocation；all-settled后恢复。每个节点可经历 Tool、Confirmation、Clarification、Checkpoint与新 Runner。
9. 节点结果使假设失效时，Owner要求收敛：未开始节点 blocked/abandoned，进行中项尽力取消；当前 Plan仍 completed + partial/failed。后继 Plan引用旧 Plan/Artifact并重新确认。

### 结算

10. 所有节点有明确结果或 Owner/用户提前收敛时进入 settling，创建 Owner汇总 WorkItem（仍使用确认时固定版本）。
11. Owner生成统一交付 Message，列出成功、失败、跳过、取消、未完成与 Artifact；只把权威Run更新为completed+outcome，Plan definition保持frozen。
12. 通知跟随原 Session来源；Memory Write只针对实际执行各 Agent的完成 WorkItem，异步失败不改 Plan结果。

## 7. Group 时序

### Formation

1. 用户在现有 UserSession选择 Group；API同事务创建 `GroupSession(forming)`、`GroupFormationProposal(generating)`、`Run(mode=group,execution_kind=proposal_generation)`、生成 WorkItem/Runner，通过proposal_id关联。forming GroupSession不可接受群消息或执行。
2. 入口 Agent只读取自己可见的 Chat历史，生成 goal、Bootstrap Context、参与节点、初始职责/WorkItem和建议边；服务端写一份不可变 revision 及 participant/work_item/suggested_edge/bootstrap_source 关系行，Proposal转 awaiting_confirmation，generation Run以completed+succeeded结算。核心图不保存为JSON，等待用户确认不占用或复活generation Run。
3. 平台只允许 `group_enabled=true`、published、用户可访问的 Agent进入 Proposal；完整旧 Chat不复制。用户每次编辑都携带 If-Match与expected_revision，复制当前关系行形成新 revision并前移 current_revision。拒绝/取消/失败把forming GroupSession关闭，不创建节点或GroupRun。
4. 确认事务锁父Session、generation Run、Proposal、forming GroupSession和current revision，重校验版本/访问/Sandbox；把GroupSession转open并冻结Bootstrap/source、GroupAgentNode、确认时建议边，创建初始GroupRun/Run和该轮AgentVersion映射，把初始WorkItem定义复制为正式WorkItem/Runner，再把Proposal标confirmed。建议边只用于展示；实际 collaboration edge 仅在后续首个invoke_agent事务中出现。后续禁止增删节点。

### 共享执行

5. 每个 Agent使用同一 GroupSession/Workspace，但 Context/History投影先按 `group_id + group_node_id`过滤。其直接可见记录只含 direct recipient 或本节点 private-owner 范围；普通group事件不自动注入。
6. 用户消息必须携带 `recipient_group_node_id`；无接收者只路由入口节点。仅接收节点创建 WorkItem/Runner。父 UserSession只写状态卡/摘要引用。
7. 节点 Clarification直接面向发起人，回答只恢复提问节点；问题/回答按recipient记录，不自动注入其他节点。
8. 图内 `invoke_agent` 创建 AgentInvocation、目标节点新 WorkItem与实际 collaboration edge；调用方等待，目标定向返回。目标版本取本 GroupRun固定映射，不能调用图外 Agent。
9. `search_group_history` 仅当显式装配时可调用，使用统一 `group_shared_search` 投影，在固定max_sequence内查询group事件与调用者有权读取的direct记录；绝不返回其他节点direct/private记录。
10. 多节点并行、Tool、Sandbox/Artifact、Confirmation、Checkpoint与恢复均使用通用原语。共享 Workspace写操作由整体锁串行。

### 结算与后续

11. 初始 GroupRun owner 为入口节点，负责首次汇总但无 Supervisor 特权。全部相关 WorkItem 终态或收敛后只结算统一 Run，GroupRun 的展示状态/结果由该 Run 派生；写父 Session 状态卡/摘要和按来源通知。
12. 返回普通 Chat只切 UI，不改变 Group。取消 GroupRun只取消其Run；关闭 GroupSession取消全部非终态Run并转只读。
13. 终态后的定向追问创建新 GroupRun/Run并引用历史。短事务锁定 UserSession、GroupSession、固定节点以及按 `agent_id` 排序的相关 Agent，重新解析每个节点：可用节点固定最新已发布版本，不可用节点写 unavailable/null version。指定节点成为 owner，未指定时使用入口节点；owner 不可用则以 `group_owner_unavailable` 拒绝且不创建部分 Run，用户可改选另一可用固定节点。非 owner 不可用不阻塞本轮，但 Runtime Catalog 排除该节点并拒绝对它的新 WorkItem、Message 和 Invocation。固定节点始终保留且不可替换/删除；重新启用只在未来 GroupRun 重新解析，既有映射不变。

## 8. Tool、Artifact 与异步边界

- `read_file/write_file/execute` 都使用 SessionSandbox固定 Workspace；路径规范化在 Sandbox内完成。
- Tool输出采用统一信封并持久化在 Invocation；超大结果写 Workspace并可发布 Artifact，Invocation只存摘要/Artifact ID。
- Artifact发布必须由 Agent/Tool显式动作；中间文件不自动发布。修改发布新 Artifact并引用 parent。
- Durable callback入口只接受 invocation id、provider event id与签名；先去重，再按当前状态结算。迟到结果不恢复终态 Run。

## 9. Checkpoint 与恢复内容

Safe Checkpoint至少含：标准化对话 turn游标、已提交 Message/Event ID、未消费的 Tool/Agent最终结果 ID、Clarification/Confirmation ID、当前模型循环 step、Prompt source manifest引用和恢复原因。不得把未提交的外部副作用只存在 JSON中。

Runner lost时 control选择 WorkItem最新 `safe_to_resume` Checkpoint，创建新 Runner；成功 Invocation直接复用，不重调 Tool。无安全 Checkpoint且无法证明无副作用时 WorkItem failed或 outcome_unknown，不从Trace猜测。

## 10. 结算、记忆与通知

Settlement 先确保所有未完成项被标成明确结果，再计算唯一 Run status/terminal result；PlanExecution 和 GroupRun 不保存镜像状态。最终 Message、Artifact 关联、Run、ExecutionEvent 和 `runtime.maintenance:extract_memory` 命令同事务提交。仅当 Session `origin=channel` 且原 binding active 时，同事务创建 NotificationDelivery 与 `runtime.delivery` 命令；Web 依靠 ExecutionEvent/Required Action，API 依靠轮询/SSE，不制造 Delivery。Maintenance 内的 Memory handler 提取失败写诊断审计，Notification 失败只改变 NotificationDelivery 并写安全事件，均不改变原 Run 结果。Trace 可以晚到或丢失。
