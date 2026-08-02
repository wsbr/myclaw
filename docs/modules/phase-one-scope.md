# 第一阶段交付边界

本文是 MyClaw 第一阶段产品范围、最小闭环和明确后置事项的唯一权威来源。它定义“第一阶段必须能完成什么”，不替代各模块的详细产品与技术规则，也不表示当前代码已经完成。

## 阶段目标

第一阶段必须证明 MyClaw 可以通过统一的平台能力装配独立垂类 Agent，并让同一套 Runtime 完成 Chat、Plan 和最小可用 Group 三种模式的端到端执行。

第一阶段是 [企业智能体网络最终蓝图](enterprise-agent-network.md) 的可落地骨架，不是长期能力上限。

第一阶段不是只验证聊天界面，也不追求一次建设全部企业治理能力。验收重点是：

- Agent 装配结果可以发布、调用和追踪；
- Tool、Skill、Model、Policy、Sandbox 和 Agent 保持正交；
- Chat、Plan、Group 共用持久化执行原语；
- 用户离线、等待确认和 Worker 重建不会丢失工作状态；
- 平台能够解释每次执行实际使用的装配与结果来源。

## 必须交付的装配能力

- 创建 Agent、维护草稿、发布不可变版本、启停 Agent；
- 编辑 `name`、`description` 和唯一的 `instructions`；
- 选择一个 Model 和一个 `local` Sandbox Profile；
- 自由选择 Platform Tool 与 Skill；
- 分别选择 Context、History、Memory Write 和 Memory Recall Policy；
- 显式绑定其他 Agent；
- 配置 `group_enabled`；
- 上传、解析、同步和版本化通用 Skill 包；
- 管理 Platform Tool 的启停、平台配置和静态 Confirmation Policy。

## 必须交付的 Runtime 闭环

- UserSession、Message、Run、WorkItem 和短生命周期 Runner；
- 模型调用和流式输出；
- 持久化 ToolInvocation、Inline Tool、Durable Async Tool；
- Tool 平台重试、取消、结果未知和轻量 Confirmation；
- Skill Catalog 注入、`read_file` 渐进加载和 Runner checksum 固定；
- Session 固定 Local Sandbox、共享 Workspace 与整体读写锁；
- 轻量 Artifact 登记和下载；
- Clarification、AgentInvocation 和可恢复等待；
- Checkpoint、ExecutionEvent、基础 OTel Trace；
- Database Outbox、Worker 租约、心跳、fencing token 和乐观并发；
- Web 断线重连以及 API 轮询、SSE 和幂等状态操作。

## 三种模式必须同时闭环

### Chat

- 用户与当前 Agent 连续对话；
- 每次新 Run 使用 Agent 最新发布版本；
- 当前 Agent 可以通过显式 `invoke_agent` 调用直接绑定 Agent；
- Clarification、Tool Confirmation、异步 Tool 和 Artifact 均可恢复。

### Plan

- Owner Agent 生成 Work Item Graph；
- 用户必须查看、编辑或确认；
- 图和参与 Agent 集合确认后冻结；
- 严格星形 `invoke_agent`；
- 节点可以等待、失败、重试和形成部分成功结果；
- Owner 汇总并向原 UserSession 交付。

### 最小 Group

- 从现有 UserSession 生成 GroupFormationProposal；
- 用户查看、编辑或确认参与图和 Bootstrap Context；
- 创建共享 GroupSession，参与 Agent 集合固定；
- 用户可以定向任一节点发送消息或回复 Clarification；
- 图内 Agent 通过 `invoke_agent` 对等协作；
- 其他节点记录只通过显式查询 Tool 获取；
- 用户可以返回普通 Chat、取消 GroupRun 或关闭 GroupSession；
- 根节点汇总初始 GroupRun，后续追问创建新的 GroupRun。

第一阶段不实现自动执行 Group、动态扩群、执行中改图、群聊全文自动注入或复杂图优化。

## 第一阶段入口

必须保证：

- Web；
- 绑定入口 Agent 的 Agent API Key；
- 持久异步 Run API；
- ExecutionEvent 查询与 SSE；
- Plan / Group 图确认；
- Clarification 和 Tool Confirmation；
- Run 取消与 Artifact 下载。

Channel 保留权威接口和 Session 来源模型，但飞书、QQ、微信等完整插件生态不作为第一阶段闭环的验收前提。

## 第一阶段内置目录

### Sandbox

- `local`

### Context Policy

- `context.standard`

### History Policy

- `history.recent`
- `history.summary_recent`

### Memory Write Policy

- `memory_write.none`
- `memory_write.user_agent_facts`
- `memory_write.user_facts`

### Memory Recall Policy

- `memory_recall.none`
- `memory_recall.user_agent`
- `memory_recall.user_combined`

### 最低 Platform Tool

- `read_file`
- `write_file`
- `execute`
- `invoke_agent`
- `search_group_history`

部署还必须至少启用一个可用 Model Provider。具体业务 Tool 不在本文枚举，由产品验收场景选择。

## 明确后置

- MCP；
- Remote / AIO Sandbox 和不可信代码安全隔离；
- 外部 MQ 与复杂队列治理；
- Fork、确定性 Replay 和历史环境保留；
- Agent 跨用户进化；
- 动态 Tool 风险判断、批量预授权和垂类审批流；
- 高级 API Key Scope；
- 预算、成本告警和复杂公平调度；
- Kubernetes 高可用生产方案；
- Skill 上传安全扫描和信任等级强制；
- 完整 Channel 插件生态和账号级通知路由；
- 面向岗位工作人员、责任人和审批人的定向用户等待；
- 个人、岗位、业务域 Agent 的高级组织归属与交接治理；
- 独立 Artifact Store。

后置不代表废弃。相关设想在 [待确认旧设计](../pending/legacy-designs-to-review.md) 中维护，不能反向扩大第一阶段范围。

## 第一阶段端到端验收

至少覆盖以下场景：

1. Builder 装配并发布一个使用 Model、Tool、Skill、Policy 和 Local Sandbox 的 Agent。
2. 普通用户在 Chat 中执行包含文件、异步 Tool、Confirmation 和断线恢复的任务。
3. 用户创建 Plan，修改并确认图，Owner 并行调用已声明 Agent，最后以部分成功或成功结算。
4. 财务 Agent 创建包含运营和订单 Agent 的 Group；节点定向澄清、二次调用、显式查询共享记录，并拒绝图外 Agent。
5. Agent 发布新版本后，新 Chat Run 使用新版；已确认 Plan / Group 继续使用确认时固定的参与 Agent 版本。
6. Tool 管理员修改启停和 Confirmation Policy 后，Runtime 按安全的即时生效规则处理新调用与尚未执行的调用。
7. Worker 在 Outbox 已领取、Runner 执行或等待恢复阶段重启，系统不重复推进同一 WorkItem，也不丢失可靠事件。
