# 第一阶段交付边界

本文是 MyClaw 第一阶段产品范围、验证假设、最小闭环和后置事项的唯一权威来源。第一阶段采用单节点受控 UAT 验证 Agent-native 架构，不表示当前代码已经完成，不是企业生产发布承诺，也不限制终局动态工作网。

## 第一阶段要证明什么

第一阶段证明五件事：

1. **完整 Agent**：个人、项目管理、产品、开发和测试 Agent 具有真实且可区分的专业行为；
2. **一个 Agent Loop**：Chat、Plan、Group 使用同一模型—Tool—观察—继续循环；
3. **模式只是策略**：Chat 可自主规划，Plan 是实施前只读检查点，Group 增加工作网协作；
4. **可信 Harness**：Tool、等待、Approval、恢复、取消、预算和副作用不会因补丁协议发生漂移；
5. **受控 Agent 社会**：用户从工作网选定三个 Agent 后，它们能在没有模板、Supervisor 和固定 DAG 的情况下真实协作。

第一阶段不追求复杂业务连接，也不以架构对象数量作为完成度。

## 固定什么，不固定什么

为了让 UAT 可复现，第一阶段固定：

- UAT 模型、Provider 配置、AgentVersion 和 clean SHA；
- 容量风险计算器场景、输入、预期结果和 Artifact；
- Plan UAT 可以发现的产品、开发、测试候选范围；
- Group UAT 从工作网选定的产品、开发、测试成员；
- 本次 GroupRun 的成员 AgentVersion 快照；
- Local Sandbox、Workspace、Tool Governance 和硬预算。

第一阶段不固定：

- Chat 是否产生 WorkingPlan；
- 模型的步骤、Tool 顺序和重复验证次数；
- WorkingPlan 的 item、顺序和 revision；
- 项目经理实际调用谁、调用几次；
- Group 谁先工作、谁向谁发消息、实际 Interaction Graph；
- 成员创建或认领哪些 SharedTask；
- 返工次数、专业判断和分歧；
- 最终自然语言表达。

固定成员来自工作网的本次 RunSnapshot，不来自 Group 模板或代码角色槽位。

## 必须交付的 Agent 装配

- Agent Draft、Draft Test、Publish、停用和版本历史；
- 身份、服务对象、专业边界和完整 Instructions；
- Model、Platform Tool、Skill、Context/Memory Policy 和 Local Sandbox；
- 工作网可见范围、受信候选和 AgentCard；
- 发布前 Model/Tool/Skill/Context/Sandbox 能力检查；
- 本次 Run 使用的 AgentVersion 和能力快照可查询。

AgentVersion 发布必须冻结精确 SkillVersion/checksum；Skill 后续发布新版本不能改变已发布 AgentVersion 或其新 Runner。

性格、耐心、严谨程度和协作习惯写入 Instructions、Memory 或 Skill，不要求新增强制 Profile 表。

## 必须交付的统一 Runtime

- UserSession、Run、WorkItem、Runner 和 Checkpoint；
- AgentTurn、ProviderAttempt、普通 Assistant Message、native Tool Call 和 stop reason；
- CapabilitySnapshot 与真实 effective Tool 集合；
- ToolInvocation / ToolAttempt、AgentInvocation、Clarification 和 Approval；
- 任意模式可用的 WorkingPlan / PlanUpdate；
- `plan_readonly` 与批准后的权限切换；
- Agent Network 查询、GroupSession、Membership、GroupRunSnapshot 和 GroupMessage；
- Agent Directory 从已发布 AgentVersion 的 AgentCard 投影做本地发现；
- AgentInvocation 创建目标 WorkItem，由 Runtime Worker 按需运行目标 AgentVersion；
- 可选 SharedTask，不作为启动或结算前置；
- Workspace、Artifact、checksum 和下载；
- Workspace 强制 Agent 私有根与 Group shared 根权限，Artifact 发布复制为不可变快照；
- 等待、取消、超时、unknown outcome 和恢复；
- Run 级预算、no-progress 诊断和有界传输重试；
- Database Outbox、lease、heartbeat、fencing 和乐观并发；
- ExecutionEvent、SSE、OTel Trace 和跨账本诊断；
- 场景化确定性 VerificationRule。

第一阶段不得把 `GoalEnvelope`、`OutcomeClaim`、通用 Evaluator、Charter Revision 等新重对象作为交付前置。

## 三种模式必须闭环

### Chat：个人工作助手

- 用户通过自然中文提出信息整理任务；
- Agent 对缺失信息创建真实 Clarification；
- Agent 可以判断任务复杂度并选择是否更新 WorkingPlan；
- Agent 根据自然语言使用 `write_file`、`publish_artifact`；
- Tool 始终按授权可见，不要求用户说内部 key；
- 普通最终消息自然结束，不需要 Completion Envelope；
- Worker 重启后同一 WorkItem 恢复，无重复副作用；
- Artifact 可以下载并校验内容。

### Plan：项目经理 Agent

- 用户显式进入 Plan，Session 切为 `plan_readonly`；
- 项目经理读取项目、澄清并生成 WorkingPlan；
- 用户可以修改、继续讨论、拒绝或批准；
- 批准后同一 Session 恢复实施权限；
- 项目经理按需调用产品、开发、测试候选，不执行冻结 DAG；
- 执行结果、失败和协作者意见可以触发 WorkingPlan 更新；
- Tool/Agent 等待、取消和 Worker 恢复闭环；
- 程序、九例测试和 Artifact 由真实环境证明；
- 最终普通消息自然交付，不依赖强制 Evaluator。

### Group：产品、开发、测试对等协作

- 三个 Agent 先发布到工作网；
- 用户从受 visibility、发布状态和 `group_enabled` 过滤的工作网直接选择三者创建 Group；
- 不经过角色模板或模型 Formation JSON；
- GroupRunSnapshot 固定本轮三个 AgentVersion；
- 每个成员使用自己的 Instructions、Tool、Memory 和 WorkingPlan；
- 成员可以直接消息、相互调用、反向复核和按需维护 SharedTask；
- 用户可以直接 @任一成员；
- 图外 Agent 邀请在 `fixed_for_run` 下形成可审计拒绝或等待；
- 真实 Interaction Graph 从消息和 Invocation 产生；
- 程序、精确测试、Artifact、失败和异议如实展示；
- Group 没有 pending action 后自然交付，不依赖通用 OutcomeClaim。
- 每个 GroupRun 固定 Reporter；Coordinator 以 summary epoch 幂等唤醒最终汇总，GroupRun 生命周期只读取一对一 Run。

## 第一阶段五个 UAT Agent

| Agent | 主要模式 | 专业定位 | 最小能力边界 |
| --- | --- | --- | --- |
| 个人工作助手 | Chat | 信息整理、澄清和行动清单 | ask_user、write、publish |
| 项目经理 | Plan | 调查、计划、协调、风险和交付 | read/search、update_plan、ask_user、invoke_agent；批准后获得实施所需能力 |
| 产品经理 | Group / Plan 协作者 | 需求、口径、成功标准和验收 | ask_user、read/write、publish、invoke_agent、group message/history |
| 开发工程师 | Group / Plan 协作者 | 实现、自检和修复 | read/write、execute、publish、invoke_agent、group message |
| 测试工程师 | Group / Plan 协作者 | 精确用例、执行、反证和复核 | read/write、execute、publish、invoke_agent、group message/history |

这些是部署和 UAT Fixture，不是代码内置角色。平台不根据名称赋权。

## 固定业务场景

唯一主场景为“小型软件项目开发组交付 Sprint 容量风险计算器”：

```text
utilization_percent = committed_hours / total_capacity_hours * 100
low: utilization < 75
medium: 75 <= utilization <= 90
high: utilization > 90
over_capacity_hours = max(committed_hours - total_capacity_hours, 0)
total_capacity_hours > 0
committed_hours >= 0
```

CLI 接受 `--capacity` 与 `--committed`，成功和失败都输出 JSON；非法参数退出码为 2。

精确用例：

| ID | capacity | committed | 预期 |
| --- | ---: | ---: | --- |
| T01 | 120 | 138 | 115%、over 18、high、exit 0 |
| T02 | 100 | 74 | low、exit 0 |
| T03 | 100 | 75 | medium、exit 0 |
| T04 | 100 | 90 | medium、exit 0 |
| T05 | 100 | 91 | high、exit 0 |
| T06 | 100 | 0 | low、exit 0 |
| T07 | 0 | 10 | capacity error、exit 2 |
| T08 | 100 | -1 | committed error、exit 2 |
| T09 | 100 | 缺失 | missing committed、exit 2 |

近似输入、参数反转或模型文字声称不能替代任何一例。

最终 Artifact：

- `requirements.md`；
- `capacity_risk.py`；
- `test_report.md`；
- `acceptance_report.md`。

## Provider 最低要求

部署中的每个目标 Provider 必须通过 Conformance Suite：

- streaming；
- native Tool Calling；
- 多轮 Tool Result 回传；
- 普通最终消息；
- stop reason；
- usage；
- Tool 参数 schema 错误反馈；
- 零 payload 传输重试；
- Context 长度和 Compaction；
- Plan、Group 所需的 Runtime Coordination Tools。

不要求 Provider 支持整段结构化 Conversation Completion。Provider 不满足最低协议时必须标记不兼容，不能静默换模型或退化为文字伪执行。

## 第一阶段入口和 UI

必须提供：

- Web；
- 绑定入口 Agent 的 API Key；
- 绑定入口 Agent 的 API Key + `X-MyClaw-User-Id`；明确接受 Key 可冒充部署内任意 userId 的一期 UAT 风险，并记录 `identity_profile=raw_user_id_v1`；
- 持久异步 Run API；
- ExecutionEvent 查询与 SSE 续读；
- Chat/Plan 模式切换和当前 permission mode；
- WorkingPlan 面板和批准/继续规划；
- Agent Network 搜索、成员选择和 Group 创建；
- Group 成员、消息、状态、计划和真实 Interaction Graph；
- Clarification、Approval、Tool Confirmation 和 Run 取消；
- Workspace/Artifact 查询、下载和 Verification 状态。

完整外部 Channel 生态不是本阶段发布前提。

## 最低 Tool 和 Runtime Coordination

Platform Tool：

- `read_file`；
- `write_file`；
- `execute`；
- `publish_artifact`；
- `search_group_history`。

Runtime Coordination Tool：

- `update_plan`；
- `ask_user`；
- `discover_agents`；
- `invoke_agent`；
- `invite_agent`；
- `send_group_message`；
- `update_shared_task`（可选）。

这些 Tool 均不能承担 `finalize_response` 语义。

## 明确后置

- Group Run 中自动批准新成员；
- 企业全网语义推荐、信誉排序和跨组织联邦；
- 外部知识库和真实业务系统；
- MCP；
- Remote/AIO Sandbox 和不可信代码强隔离；
- Kubernetes HA 和外部 MQ；
- 企业生产 SLO、容量证明、HA、已承诺 RPO/RTO 和完整威胁模型闭环；
- 复杂业务审批与高级 API Scope；
- 自主 Skill 蒸馏和跨用户学习；
- 独立 Artifact Store 和确定性 Replay；
- 通用文本质量 Evaluator 市场。

后置不代表否定终局。Member Event、AgentCard、权限、Context 和 Invocation 数据模型必须允许后续渐进开放。

## 第一阶段 UAT 门禁

1. 固定模型和 clean SHA 上，三模式使用同一 Agent Loop 端到端通过；
2. Chat 复杂任务能够按需更新 WorkingPlan，简单任务不被强制规划；
3. Plan 在批准前没有写入/副作用，批准后同一 Session 动态实施；
4. Group 成员确实从工作网选择，不存在模板或硬编码角色赋权；
5. Agent 空闲时不要求常驻 Runner；本地委派和 Group 调用复用同一 AgentInvocation 调度路径；
6. 九例以精确 ToolAttempt argv、输出和退出码证明，必须 9/9；
7. 四个 Artifact 存在、可下载且 checksum/内容正确；
8. Tool 可以按业务需要重复使用，不因成功一次消失；
9. 不存在 `finalize_response`、`required_tool_keys` 或复杂最终结构化合同依赖；
10. 文字声称不能伪造 Tool、测试、Artifact 或 verified 状态；
11. Clarification、Approval、Agent 等待、取消和 Worker 重启可恢复；
12. 图外邀请拒绝、权限拒绝、失败、unknown outcome 和迟到结果可审计；
13. 无重复业务副作用、无终态复活、无旧 lease 写入；
14. Chat 10/10、Plan 30/30、Plan 全流程 5/5、Group 建组 10/10、Group 全流程 5/5 达到阈值；
15. UAT 环境、AgentVersion、模型、数据、Trace 和 Artifact 可复现、可对账。

以上门禁只说明第一阶段 UAT 完成。企业生产发布还必须单独满足[生产就绪要求](../operations/production-readiness.md)，包括明确且实测的 SLO、容量、RPO/RTO、HA、威胁模型和演练证据。
