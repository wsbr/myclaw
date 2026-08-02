# MyClaw 产品设计总览

> 本文是产品入口，只概述目标产品与用户体验。完整定义以 [设计文档索引](docs/README.md) 中的权威模块为准。

## 产品定位

MyClaw 是垂类 Agent 装配平台。平台统一提供模型、Tool、Skill、上下文、记忆和协作 Runtime，装配者通过选择这些能力构建能够独立承担领域工作的 Agent。

MyClaw 不把 Agent 简化为聊天提示词，也不要求每个 Agent 重复建设文件读取、模型调用、记忆、长任务恢复和协作机制。平台负责复用、授权和可靠执行，Agent 负责领域判断与语义结果。

长期目标是形成企业智能体网络：每个垂类岗位、业务域乃至个人都可以拥有完整 Agent，并在企业网络中发布、更新、协作和下线。人发起目标后，Agent 优先承担领域工作和跨域协调，确实缺少信息、判断或授权时才定向找人。完整北极星见 [企业智能体网络最终蓝图](docs/modules/enterprise-agent-network.md)。

## 核心产品对象

- **Agent**：稳定的垂类身份；具体可执行装配以不可变 AgentVersion 发布。
- **Enterprise Agent Network**：企业内可发现、可治理、可协作的个人、岗位、业务域和公共 Agent 网络。
- **Platform Tool**：由平台代码实现和注册的动作能力。
- **Skill**：兼容通用 `SKILL.md` 的可移植能力包。
- **Model**：由平台统一管理、由 Agent 固定选择的模型。
- **Policy**：平台内置的上下文、历史和记忆策略。
- **Sandbox**：Agent 选择、Session 固定的执行环境与共享工作空间。
- **Artifact**：Session Workspace 中正式发布文件的稳定引用。
- **UserSession**：用户与入口 Agent 的连续对话容器。
- **Work Item**：可跨时间持续的业务工作。
- **Runner**：一次短生命周期、不可原地恢复的 Agent 执行尝试。
- **Plan**：经用户确认并冻结的中心化 Work Item Graph。
- **GroupSession**：固定参与者共享的对等 Agent 群组会话。
- **GroupRun**：GroupSession 内一次不可重启的具体工作周期。
- **Channel**：将第三方机器人会话绑定到 UserSession 的双向接入能力。

## Agent 装配体验

Agent 配置页按以下区域组织：

1. **基础信息**：名称、任务描述和状态。
2. **模型与指令**：选择一个模型和 Sandbox，编辑唯一的 Agent Instructions。
3. **能力装配**：自由选择 Tool 与 Skill。
4. **上下文与记忆**：分别选择四类平台策略。
5. **协作与访问**：绑定可发现 Agent，并决定是否启用 Group。

Chat 和 Plan 是所有可交互 Agent 的固有模式；Group 只有在 `group_enabled` 后才可选。Agent 页面不重复配置 Tool 实现、模型采样参数或策略内部阈值。

Agent 编辑采用草稿—发布模型。Chat 每个新 Run 使用最新已发布版本；Plan 和 Group 在用户确认图时固定参与 AgentVersion，执行期间不随管理面编辑改变。

## 三种执行模式

| 维度 | Chat | Plan | Group |
| --- | --- | --- | --- |
| 适用目标 | 即时和开放式对话 | 可展示、可恢复的长任务 | 多领域对等协作 |
| 中心 | 当前 Agent | Plan Owner | 无固定 Supervisor |
| 图节点 | 无正式图 | Work Item | Agent 参与实例 |
| Agent 调用 | 中心 → 子 Agent | Owner → 已声明子 Agent | 图内任意节点互调 |
| 用户沟通 | 当前 Agent | 仅 Owner | 群内任意 Agent |
| 图确认 | 无 | 必须 | 必须 |

### Chat

当前 Agent 统一与用户沟通，可以通过 `invoke_agent` 调用直接绑定 Agent。子 Agent 不能继续调用其他 Agent，也不直接向用户澄清。

### Plan

当前用户交互 Agent 成为 Plan Owner，生成 Work Item Graph。用户确认或修改后图完全冻结；Owner 采用严格星形拓扑调用计划中已经声明的子 Agent。

执行结果导致计划失效时，旧 Plan 以已有结果结算，Owner 生成需要重新确认的后继 Plan，而不是修改运行中的图。详见 [Plan 模式](docs/modules/plan-mode.md)。

### Group

入口 Agent 从当前 UserSession 提取待共享 Bootstrap Context，生成参与 Agent 图。用户确认后建立共享 GroupSession，参与者集合固定。

Group 中所有对话进入同一共享会话。每个 Agent 的默认可见上下文由 `group_id + agent_id` 确定；其他节点的共享记录不会自动注入，只能通过显式装配的查询 Tool 获取。Agent 之间的执行请求仍只能通过 `invoke_agent`。

Group 的唯一人类参与者是任务发起人。某个垂类 Agent 后期可以定向联系岗位工作人员、责任人或审批人，但这些人不会加入 Group，也不会自动看到群组上下文；回复只恢复请求 Agent 的工作。

群组可以包含多个 GroupRun。终态 GroupRun 不重新激活；用户在同一目标下追问时创建新的 GroupRun。详见 [Group 模式](docs/modules/group-mode.md)。

## 可恢复执行体验

Work Item 可以等待用户、Agent 或异步 Tool，而不占用长期进程。等待条件满足后，平台在同一个 Work Item 下创建新 Runner 继续。

所有 Tool 调用持久化为 ToolInvocation，并区分即时与持久化异步。平台只在同一 ToolInvocation、相同参数和幂等键内处理瞬时技术重试；Agent 只看到最终状态。

取消表示停止后续推进并尽力取消运行操作，不承诺回滚已产生的业务副作用。

节点成功、失败、超时、取消或放弃都属于明确结果。Run 是否完成与业务是否成功分开表达；部分节点失败后仍可汇总已有结果并以部分成功或失败完成，不会因为等待项永久卡住。

## 文件与执行环境

Agent 显式选择平台 Sandbox Profile。第一阶段只有物理机目录隔离的 `local`，Session 创建后固定并共享一个 Workspace。Workspace 文件操作使用平台读写锁；无法预知写入范围的 `execute` 独占 Workspace。

Skill 脚本当前默认可信，可以由显式装配 `execute` 的 Agent 在 Local Sandbox 中运行。Local Sandbox 不被宣传为不可信代码安全边界。

正式交付文件登记为轻量 Artifact。文件仍保存在 Session Workspace，Artifact 只提供稳定 ID、来源、版本和下载引用。

## 账号与外部访问

平台以企业内唯一 `userId` 识别用户。Web、API、Channel 和 Agent 调用链共用同一身份；修改 `userId` 视为新人，不迁移或合并历史数据。

同一 Agent 可以创建多把独立撤销和轮换的 API Key。API 使用持久异步 Run 协议，支持轮询、SSE、图确认、定向澄清、Tool Confirmation、取消和 Artifact 下载，不要求外部团队使用平台前端。

## Channel

平台账号可以开通飞书、QQ、微信等 Channel，并把第三方机器人绑定到入口 Agent。外部 conversation 首次消息创建 Session，之后持续复用绑定 Session。

第一阶段核心闭环不以完整 Channel 插件生态为验收前提；已经接入 Channel 时，通知严格跟随 Session 来源：Channel 中产生的任务将完成、失败、澄清和确认通知投递回原 Channel conversation；Web 通过站内事件展示，API 通过查询或 SSE 获取。后期才提供账号级默认通知渠道或全渠道设置。

## Tool Confirmation

Platform Tool 列表页允许管理员启停 Tool，并在 `never | always` 之间调整静态 Confirmation Policy。Agent 只能选择 Tool，不能覆盖平台策略。需要确认时提供类似命令行执行确认的轻量交互：

```text
Agent 准备执行某 Tool 与精确参数
→ 用户确认或拒绝
→ 确认后执行原 ToolInvocation
```

策略从 `never` 收紧为 `always` 时，尚未执行的调用必须等待确认；放宽策略不会自动批准既有等待。平台前端不是唯一入口，外部 API 也可以获取并响应 Confirmation。

## Group 页面与退出

Group 是当前 UserSession 中的子空间。用户可以随时返回入口 Agent 的普通 Chat，Group 在后台继续。

以下操作严格区分：

- **返回 Chat**：只切换消息作用域；
- **取消当前 GroupRun**：停止该轮工作；
- **关闭群组**：取消非终态工作并将 GroupSession 变为只读。

父 UserSession 只展示 Group 状态卡和结果摘要引用，不复制完整群聊。输入框必须明确当前消息发送到普通 Chat 还是某个 Group Agent。

## 权限边界

- 第一阶段平台角色为 `user / builder / admin`，Agent 范围为 `private / enterprise`。
- Builder 拥有 Agent 蓝图，不拥有使用者的 Session、Memory、Artifact 或业务内容。
- Runtime 不静默补齐未选择的 Tool。
- Skill 不扩大 Tool、MCP 或 Agent 权限。
- 会话和请求不能临时切换 Agent 模型。
- 其他 Agent 不能读取本 Agent 的 Instructions、私有记忆或内部 Prompt。
- Agent 间不存在隐藏执行通道，只有 `invoke_agent`。
- Agent 调用代表原始执行主体，目标 Agent 带来领域能力但不带来身份提权。
- 平台服务凭证不等于用户拥有全量业务数据权限。

## 文档导航

- [企业智能体网络最终蓝图](docs/modules/enterprise-agent-network.md)
- [第一阶段交付边界](docs/modules/phase-one-scope.md)
- [设计哲学与产品原则](docs/DESIGN_PHILOSOPHY.md)
- [Agent 装配](docs/modules/agent-assembly.md)
- [Platform Tool](docs/modules/tool-system.md)
- [Skill](docs/modules/skill-system.md)
- [Model](docs/modules/model-system.md)
- [上下文与记忆](docs/modules/context-memory.md)
- [身份与 Agent API](docs/modules/identity-api.md)
- [Sandbox 与 Artifact](docs/modules/sandbox-artifact.md)
- [Channel 与通知](docs/modules/channel-system.md)
- [Agent 协作](docs/modules/agent-collaboration.md)
- [Plan 模式](docs/modules/plan-mode.md)
- [Group 模式](docs/modules/group-mode.md)
- [Runtime 执行](docs/modules/runtime-execution.md)
- [Runtime 调度与数据库并发](docs/modules/runtime-scheduling.md)
- [执行事件与可观测性](docs/modules/observability.md)
- [技术设计总览](TECH.md)
