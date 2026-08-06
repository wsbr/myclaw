# Platform Tool

本文是 Tool 注册、治理、装配和有效权限的唯一权威来源。

## 定义

Platform Tool 是由 MyClaw 后端代码实现、注册并通过 Runtime 调度的环境或业务动作能力。平台只有一个 Platform Tool Registry，不区分 Built-in Tool 和数据库自定义 Tool 两套体系。

`read_file`、`write_file`、`execute`、`search_group_history`、`publish_artifact` 是 Platform Tool。它们可能由核心代码提供，但不会因此被隐式加入 Agent。

模型还可以通过相同的原生 tool-call 协议调用 `update_plan`、`ask_user`、`discover_agents`、`invoke_agent`、`invite_agent` 和 Group Message 等 **Runtime Coordination Tool**。这类能力由 Runtime 按当前模式、身份和协作关系注入，用于改变真实计划、等待或协作状态；它们不进入 Platform Tool Registry，也不能由 Agent 装配者选择、替换或借此扩大业务权限。两类能力共用调用协议，但治理来源和副作用语义不同。

## 注册与治理

Tool 实现随平台代码部署。注册信息至少包含：

```text
key
name
description
input_schema
output_schema
implementation_ref
platform_config
execution_mode
enabled
confirmation_policy
version
```

用户和 Agent 装配者不能上传、创建或修改 Tool 实现。管理员页面可以：

- 查看注册元数据和 Schema；
- 启用或停用；
- 在 `never | per_call | session_grant` 之间切换 `confirmation_policy`；
- 维护平台级连接或运行配置；
- 执行连通性测试；
- 查看哪些 Agent 正在使用。

平台级配置由 Tool 实现解释，不复制到 Agent 记录。

管理员还可以编辑展示名称和说明。`key`、输入输出 Schema、实现类型和代码入口来自平台代码注册，不能在页面创建、上传或改写。Agent 装配者不能修改 Tool 的启停、Confirmation Policy 或平台连接配置。

## Agent 装配规则

Agent 页面仅选择 Tool key，不允许 Agent 级参数覆盖。必须区分装配集合和某一 Model Turn 的有效集合：

```text
assembled_tools = resolve(agent_version.tool_keys)
effective_tools = assembled_tools
                  ∩ enabled_registry_tools
                  ∩ execution_principal_permissions
                  ∩ current_permission_mode
                  ∩ current_collaboration_or_membership_scope
                  ∩ remaining_hard_budget
```

Runtime Coordination Tool 不包含在 `assembled_tools` 中，其有效集合由当前 `permission_mode`、调用主体、Session/Group Membership、协作策略和预算独立计算。

Runtime 不依据用户消息措辞、Skill 内容、模型文本、Group 开关或历史使用自动添加 Tool。自然语言不需要包含内部 Tool key；相同机器状态下，改写、翻译或标点变化不能改变有效权限。

当显式选择的 Tool 不存在或被停用时，Runner 应明确失败或按平台定义拒绝启动，不能静默删除 Tool 后继续造成能力漂移。

## 调用边界

模型只能看到本 Turn 有效的 Platform Tool 与 Runtime Coordination Tool 定义，也只能调用该集合。Platform Tool Dispatcher 在每次调用时再次校验：

- Tool 在 Runner 快照中；
- 输入符合 Schema；
- 平台级配置可用；
- 调用满足该 Tool 自身的运行时约束。

Runtime Coordination Tool 的执行器必须再次校验身份、模式、Membership 与目标范围，并直接写入相应领域状态和 Event。Harness 不向模型暴露用于定义完成条件或关闭 Tool 集合的控制 Tool。这些协调能力不能声明任务成功、定义必需 Tool 清单或关闭业务能力。Provider 的完整普通消息或 `end_turn` 自然结束当前 Turn，Run 状态继续由 pending Invocation、等待、取消和预算等机器事实决定。

任何模型可调用能力在授权期间都不是一次性槽位。某次 `write_file`、`execute`、`publish_artifact`、`invoke_agent` 或 `search_group_history` 成功后，Runtime 不得仅因“已经用过”就从后续 Turn 的有效集合中移除整个 Tool key。是否重复执行由具体 Invocation、参数、目标资源、因果 Action 和 operation key 判断。

`invoke_agent` 还必须执行 Agent 可发现范围和 Group 参与者集合校验，详见 [Agent 协作](agent-collaboration.md)。

`search_group_history` 只能查询当前 GroupSession 中对成员公开的 Message、ExecutionEvent 和 Artifact 索引。它必须校验当前 Agent 是 Group 成员，并排除其他 Agent 的 Instructions、私有记忆、Prompt、模型内部推理、未公开 Tool 数据和未发布 Workspace 文件。启用 Group 不会自动装配该 Tool。

## ToolInvocation 与执行类型

所有 Tool 调用都持久化为一个语义上的 `ToolInvocation`，底层一次或多次物理尝试记录为 `ToolAttempt[]`。

平台支持两种由 Tool Registry 定义、Agent 不能覆盖的执行类型：

```text
inline
durable_async
```

- `inline`：短时间内返回最终结果，当前 Runner 可以继续；
- `durable_async`：受理后当前 Runner 结束，Work Item 进入 `waiting_tool`；回调或轮询得到最终结果后启动新 Runner 继续。

用户离线不影响异步 Tool。`execute` 原则上按可持久化异步能力设计，避免 Skill 脚本长期占用 Runner。

访问 Session Workspace 的 Tool 还必须遵守 [Sandbox 与 Artifact](sandbox-artifact.md) 的整体读写锁。无法预知写入范围的 `execute` 在运行期间获取独占锁；等待锁的 ToolInvocation 进入 `waiting_resource`。

## 平台边界重试

平台重试只处理同一个 ToolInvocation、完全相同参数和幂等键的瞬时技术失败。Agent 看不到中间 ToolAttempt，只看到最终状态和结果。Agent 基于新事实再次调用同一 Tool 属于新的业务 Action，不是平台技术重试。

```text
ToolInvocation
├── ToolAttempt 1：瞬时失败
├── ToolAttempt 2：瞬时失败
└── ToolAttempt 3：成功
```

是否可重试由 Tool 平台实现决定。有副作用且无法确认远端是否成功时进入 `outcome_unknown`，不能盲目重放。异步回调按 `invocation_id` 去重。

## 轻量 Tool Confirmation

第一阶段由 Platform Tool Registry 提供默认策略：

```text
confirmation_policy: never | per_call | session_grant
```

- `never`：新的 ToolInvocation 直接进入可执行状态；
- `per_call`：每一个新的 ToolInvocation 都需要原始用户确认；
- `session_grant`：用户可以批准严格受限、可撤销、带到期时间的 Session Grant，范围内的后续调用不逐次确认。

策略是平台级实时治理配置，不复制到 AgentVersion，也不允许 Agent 级覆盖。Session Grant 不是 Agent 自我授权，必须绑定 `tenant_id + account_id + account_authorization_revision + session_id + tool_key + sandbox_profile_revision + arguments_constraint_hash + risk_version + expiry`；账号授权、参数、路径、网络、时间或风险版本超出范围时重新确认。第一阶段不定义金额策略、组织审批或复杂 Governance Profile。

需要确认时：

```text
ToolInvocation = waiting_confirmation
→ 用户查看 Tool 与精确参数
→ confirm | reject
→ 确认后执行同一个 ToolInvocation
```

确认记录绑定 `tool_key + arguments_hash + execution_principal`；参数变化必须重新确认。已确认调用的同参数平台重试不重复确认。等待确认不占用 Runner，拒绝以 `rejected` 返回 Agent。

平台前端只是默认客户端。外部服务可以通过标准 API 查询 required action 并提交确认或拒绝。Instructions 或 Skill 可以要求 Agent 进行业务上的对话确认，但不能替代 Tool 执行边界的 Confirmation。

只有请求 AuthContext 的 `tenant_id + account_id` 与 ToolInvocation 固定主体相同的调用可以确认或拒绝。Web/生产 OIDC 由已验证身份解析该账号；第一阶段 Agent API 由 `X-MyClaw-User-Id` 解析，因此 Key 持有者可以冒充该用户确认，这是当前明确接受且阻断生产就绪的风险。Builder 和管理员可以停用 Tool 或取消异常执行，但不能通过管理角色代替用户批准业务操作。

等待确认时，Runner 保存 Checkpoint 后结束，WorkItem 进入可恢复等待。第一阶段默认不自动超时拒绝；用户可以稍后确认、拒绝或取消执行。

### 配置变更的即时语义

Runtime 在底层执行前必须重新读取 Tool 当前启停和 Confirmation Policy，采用以下安全非对称规则：

- 任意策略收紧为 `per_call`：尚未开始底层执行且没有精确批准的 ToolInvocation 转为 `waiting_confirmation`；
- `per_call → never`：新的 ToolInvocation 不再等待，但既有 `waiting_confirmation` 不会被自动批准；
- 切换为 `session_grant`：新的调用只有命中有效 Grant 才可执行；既有等待不会仅因策略变化自动批准，用户必须显式批准本次调用或创建并绑定 Grant；
- Grant 撤销、过期或风险版本变化：尚未开始且依赖该 Grant 的调用重新进入确认；已开始的调用不承诺回滚；
- Tool 停用：拒绝新调用，尚未开始执行的调用以 `tool_disabled` 结算；
- 已经进入底层执行的调用不承诺强制中断或回滚；
- 重新启用 Tool 不复活已经失败、拒绝或取消的 ToolInvocation。

首批注册默认值：

| Tool 类型 | 默认策略 |
| --- | --- |
| `read_file`、只读查询、登记当前用户 Session 内 Artifact | `never` |
| Local Workspace 的 `write_file`、`execute` | `session_grant` |
| 外部不可逆副作用 Tool | `per_call` |

管理员可以在 Tool 列表页根据部署环境调整。后期动态风险判断和垂类审批边界保留在 [待确认旧设计](../pending/legacy-designs-to-review.md)。

`invoke_agent` 的允许与否由 Agent Network 可发现范围、Group Membership、当前 Collaboration Policy 和预算决定，不套用 Platform Tool 的 Confirmation Policy；若未来某类跨组织邀请需要人工审批，应创建明确的 Approval，而不是把它伪装成 Tool 装配配置。

## 取消

取消采用“停止继续推进”，不承诺事务回滚：

- 停止创建后续 Runner 和新调用；
- 对支持取消的异步 Tool 尽力发送取消请求；
- 已完成的副作用不回滚；
- Tool 不支持取消时不能伪装成已取消；
- 取消与完成并发时保留真实结算结果。

## 与 Skill 的关系

Skill 可以指导 Agent 调用某个 Tool，也可以携带由 `execute` 运行的脚本，但这只是操作说明。Skill：

- 不声明 Tool 依赖以触发自动安装；
- 不把 Tool 加入有效集合；
- 不扩大 Tool 输入范围或平台授权；
- 不保证 Agent 已装配它所描述的 Tool。

当前阶段平台不判断装配组合是否合理，由装配者负责选择。确定性 Verification 或可选 Reviewer 可以指出缺少的事实，但不能要求固定 Tool key、临时添加 Tool，或把 Tool 使用历史本身当作业务成功。
