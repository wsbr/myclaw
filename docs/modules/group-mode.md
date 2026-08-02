# Group 模式

本文是 Group 的产品语义、共享会话、参与图、用户交互和生命周期的唯一权威来源。

## 定义与模式边界

Group 是多个平等垂类 Agent 围绕一个用户目标建立的临时协作群。入口 Agent 是用户最先对话并生成组图的 Agent，它仍是普通垂类 Agent，只在初始目标中承担根节点和首次交付职责。

Group 只有一个人类参与者，即任务发起人。后期某个垂类 Agent 可以定向等待其他岗位工作人员、责任人或审批人的回复，但目标人不加入 Group、不成为节点，也不自动获得 Group 记录。长期产品边界见 [企业智能体网络最终蓝图](enterprise-agent-network.md)。

Group 与 Chat、Plan 的边界：

| 维度 | Chat | Plan | Group |
| --- | --- | --- | --- |
| 协作拓扑 | 中心 Agent + 子 Agent | Plan Owner + 子 Agent | 对等 Agent 网络 |
| 图节点 | 无正式图 | Work Item | Agent 参与实例 |
| Agent 调用 | 中心 → 直接绑定 Agent | Owner → 已声明 Agent | 任意节点 → 图内任意节点 |
| 用户沟通 | 当前 Agent | 仅 Plan Owner | 群内任意节点 Agent |
| 图确认 | 无 | 必须确认 | 必须确认 |

Plan 和 Group 不能相互嵌套或在执行中切换。Group 只有在 Agent 配置 `group_enabled = true` 时才可被用户选择。

## 从 UserSession 建群

用户先在 `UserSession` 中与入口 Agent 进行 Chat。用户显式选择 Group，或入口 Agent 判断需要协作时，只能生成 `GroupFormationProposal`，不能自动执行：

```text
UserSession
└── GroupFormationProposal
    ├── Group Goal
    ├── Bootstrap Context
    ├── Participant Agents
    ├── Initial Work Items
    └── Initial Collaboration Edges
```

用户可以修改参与 Agent、初始职责、Work Item 和协作关系。确认时解析并固定每个参与者的当前已发布 `agent_version_id`，随后创建 GroupSession 并冻结参与者集合。Group 不提供自动执行策略，用户可以拒绝当前 Proposal，但不提供“永久禁止 Agent 建议 Group”的会话开关。

## 建群前上下文共享

入口 Agent 与用户在建群前的完整历史不会开放给其他参与者。组图时生成 `GroupBootstrapContext`，至少包含：

- 本次目标；
- 用户确认共享的必要背景；
- 已确认业务口径；
- 输入资料和 Artifact；
- 来源 UserSession Event 引用。

用户确认图时同时确认 Bootstrap Context。其他 Agent 只能获得该上下文，不能读取入口 Agent 建群前的完整 Chat 历史。

## 结构

```text
UserSession
└── GroupSession
    ├── Fixed Group Agent Nodes
    ├── Shared Messages / ExecutionEvents
    ├── GroupTranscriptDocument
    ├── Collaboration Edges
    ├── Group Artifacts
    └── Group Runs
        ├── Work Items
        ├── Clarifications
        ├── Agent Invocations
        └── Runners
```

### GroupSession

GroupSession 是群组的长期共享会话容器，保存固定参与者、共享聊天、成果索引和多个 GroupRun。它不表示某一轮任务是否正在执行。

GroupSession 状态：

```text
forming
open
closed
archived
```

`idle` 与 `busy` 根据非终态 GroupRun 数量计算，不作为可反复切换的持久化生命周期状态。

### Group Agent Node

某 Agent 在 GroupSession 中唯一的参与实例。同一 Group 中同一个 Agent 只保留一个节点。节点维护自己的 Work Item、Runner、状态游标和 Agent 私有执行数据，但不拥有独立聊天 Session。

节点上下文的稳定命名空间是：

```text
group_id + agent_id
```

完整定位可以包含父级 `session_id`。节点上下文是共享 GroupSession 的运行时投影，不是另一份对话事实源。

### GroupRun

GroupRun 是群组内一次具体、可结算的工作周期。初始用户目标、完成后的相关追问或定向补充任务分别使用不同 GroupRun。

生命周期状态包括：

```text
created
running
waiting_user
waiting_agent
waiting_tool
settling
completed
cancelled
```

完成后的业务结果单独记录：

```text
succeeded
partially_succeeded
failed
```

`waiting_*` 为非终态，可以用新 Runner 继续；`completed` 和 `cancelled` 为终态，永远不能重新激活。一个 GroupRun 可以是 `completed + failed`，表示所有工作已经形成明确结论，但业务目标没有达成。终态后的追问在同一 GroupSession 创建新 GroupRun，并通过 `reference_run_ids` 引用历史执行。

### Work Item

Work Item 是节点承担的具体工作。同一节点可以有多个 Work Item。初始图确认后节点集合固定，但节点内部可追加 Work Item。

### Collaboration Edge / AgentInvocation

边表示两个既有节点存在协作关系；具体请求由 AgentInvocation 记录。任一节点可通过显式装配的 `invoke_agent` 调用图内任意其他节点，追加 Work Item 和协作边，但不能调用图外 Agent。

### Group Artifact

Group Artifact 是发布到 Group 的轻量 Artifact 引用。文件本体仍在 Session Workspace，完整定义见 [Sandbox 与 Artifact](sandbox-artifact.md)。

## 共享 GroupSession

Group 激活后的用户消息、Agent 消息、Clarification、AgentInvocation、Work Item、Artifact 和最终交付都属于同一 GroupSession 的共享记录。Message 和领域状态是当前事实，事务一致的 ExecutionEvent 形成可查询时间线。

Group 内的定向语义是“共享可查询、定向处理”：

- 消息可以指定接收 Agent；
- 只有被指定 Agent 立即被唤醒并负责响应；
- 未被指定 Agent 不会因为消息自动启动 Runner；
- 当前 Agent 的策略只处理 `group_id + agent_id` 直接可见范围；
- 其他节点记录必须通过显式装配的 `search_group_history` 获取；
- Agent 之间执行工作仍必须使用 `invoke_agent`，不能用普通群聊消息替代调用。

共享会话不共享 Agent Instructions、Tool、Skill 内容、私有记忆、Runner Prompt、模型内部推理、凭证或敏感 Tool 原始数据。

## 共享记录与 Transcript

Group 共享记录由领域 Message 和 [ExecutionEvent](observability.md) 组成，至少保留：

```text
sequence
sender_type
sender_id
recipient_type
recipient_id
event_type
reply_to_event_id
clarification_id
work_item_id
runner_id
invocation_id
caused_by_event_id
payload
created_at
```

`sequence` 提供展示顺序，因果字段表达并发执行的真实关系。领域表保存当前状态，ExecutionEvent 保存可靠变化历史；不能用 Observability Trace 推导 Group 状态。

`GroupTranscriptDocument` 是由共享记录派生的只读文本或索引，用于摘要、全文检索和显式查询，不是状态事实源。Context / History Policy 不得从中自动扩展当前 Agent 的可见范围。平台不为 Group 隐式添加 Tool。

## 用户与节点交互

用户可以：

- 回复任一 Agent 发起的 Clarification；
- 主动选择任一节点 Agent 补充信息；
- 向任一节点询问进度；
- 查看完整群组时间线；
- 按 Agent、Work Item 或调用链过滤视图。

Clarification 仍使用稳定 `clarification_id`。问题和回答都写入共享 GroupSession，自动进入提问节点的可见范围，只有提问节点因回答被唤醒；其他节点需要显式查询。多个节点可以同时等待不同用户回答。

未指定接收者的群组用户消息默认交给初始入口 Agent。改变整体目标或需要图外 Agent 时，当前 GroupRun 应汇报无法在确认图内继续，以已有结果结算为部分成功或失败，并建议创建新的 GroupFormationProposal。

## 图语义与冻结边界

确认后固定：

- 参与 Agent 集合；
- 每个参与节点在本次 GroupRun 使用的 AgentVersion；
- 初始入口 Agent；
- Group 原始目标和访问范围。

执行中允许：

- 更新节点与 Work Item 状态；
- 图内节点追加 Work Item；
- 图内任意节点通过 `invoke_agent` 新增协作边；
- 新增 Clarification、GroupRun 和 Artifact；
- 取消未结算工作。

执行中禁止：

- 加入图外 Agent；
- 删除已经发生的节点、边、调用或消息历史；
- 把某次入口 Agent 变成系统级永久 Supervisor；
- 嵌套 Plan 或另一个 Group。

参与 Agent 在执行期间发布新版本，不改变已经确认的 GroupRun。平台级 Tool 或 Model 被紧急停用时仍按当前治理状态明确失败。终态后的新 GroupRun 若沿用同一固定参与者集合，应重新解析各参与 Agent 当时的最新已发布版本，并在该轮开始时固定。

## GroupRun 完成与后续追问

初始 GroupRun 由入口 Agent 作为 `owner_node_id`，负责汇总并向原 Session 交付，但不因此成为其他 Agent 的 Supervisor。

成功、失败、超时、取消、放弃和依赖阻塞都是 Work Item 的明确终态结果。失败结果定向返回调用方，不会让 GroupRun 永久悬挂。根节点应汇报已有成果、Artifact、失败原因和未完成内容。

正常完成时，所有相关工作形成终态结果后进入结算。用户或根节点也可以要求提前收敛：

```text
停止创建新工作
→ 尽力取消运行中的 Tool / Agent 调用
→ 未完成工作标记为 cancelled 或 abandoned
→ 保存已有结果
→ 结算为 completed + partially_succeeded / failed，或 cancelled
```

未完成工作可以阻止 `succeeded`，但不能阻止 GroupRun 结束。迟到结果记录为事件，不能重新激活终态 GroupRun。

完成后的相关追问不重新激活旧 GroupRun：

```text
GroupSession = open
├── GroupRun 1 = completed
└── GroupRun 2 = running
```

用户完成后直接 @某节点追问时，新 GroupRun 的 `owner_node_id` 是被指定 Agent；未指定时默认入口 Agent。后续请求仍只能使用固定参与者集合。新的独立目标必须创建新 GroupSession。

## 离开、取消与关闭

以下操作含义不同：

- **返回入口 Agent Chat**：只切换 UI 和消息作用域，Group 在后台继续；
- **取消当前 GroupRun**：停止该轮后续推进，GroupSession 保持 open；
- **关闭群组**：向所有非终态 GroupRun 传播取消请求，GroupSession 进入 closed 并只读。

关闭不回滚已经产生的业务副作用。Closed GroupSession 不重新打开；需要继续时创建引用旧成果的新 Group。

父 UserSession 只接收 Group 创建、等待用户、完成等状态卡和结果摘要引用，不复制完整 GroupTranscript。输入框必须明确展示当前消息发往普通 Chat 还是某个 Group 节点。

## 示例：财务目标的对等协作

用户在财务 Agent 的 UserSession 中请求解释本月毛利下降：

1. 财务 Agent 生成包含财务、运营和订单 Agent 的 Proposal，并从原 Chat 提取待共享 Bootstrap Context。
2. 用户调整后确认，创建 GroupSession 和初始 GroupRun。
3. 三个节点并行工作；订单 Agent 在共享群聊中 @用户询问退款日期口径。
4. 用户回答只唤醒订单节点；财务和运营 Agent 只有显式查询共享记录后才能取得该口径。
5. 运营 Agent 通过 `invoke_agent` 二次调用已经在图中的订单 Agent，订单节点新增 Work Item。
6. 运营 Agent 尝试调用图外供应链 Agent 时，Runtime 拒绝。
7. 相关工作结算后，财务节点完成初始 GroupRun 并交付结果。
8. 用户随后直接 @订单 Agent 追问取消订单范围，平台在同一 GroupSession 创建新的 GroupRun，而不是重新激活旧执行。
