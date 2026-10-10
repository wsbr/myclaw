# Platform Tool

本文是 Tool 注册、治理、装配和有效权限的唯一权威来源。

## 定义

Platform Tool 是由 MyClaw 后端代码实现、注册并通过 Runtime 调度的环境或业务动作能力。平台只有一个 Platform Tool Registry，不区分 Built-in Tool 和数据库自定义 Tool 两套体系。

`read_file`、`write_file`、`execute`、`publish_artifact` 是 Platform Tool。它们可能由核心代码提供，但不会因此被隐式加入 Agent。

`search_tools`、`update_plan`、`find_agent_user`、`ask_user`、`discover_agents`、`select_group_members`、`invoke_agent`、`search_group_history`、`send_group_message`、`invite_agent`、`update_shared_task` 是由 Runtime 执行的协调工具。它们同样进入 Platform Tool Registry，通过不可变 ToolVersion 和 AgentVersionTool 显式绑定；不再自动注入。工具目录和 Agent 配置页统一显示这些工具的中文名称、标识及适用模式。

Runtime 专用执行器只负责计划、等待和协作等领域状态，不构成第二套授权入口。注册使用 `namespace=runtime` 和 `origin=builtin`；`runtime_owned`、`runtime_modes` 注解用于展示，执行授权必须验证冻结的 ToolVersion 和可信代码入口，不能仅信任注解或 `source` 字段。

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
- 在工具支持的 `never | per_call | session_grant` 之间切换 `confirmation_policy`；Runtime 协调工具当前仅支持 `never`，其他设置或要求执行确认的风险规则返回 422；
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

Runtime 协调工具包含在同一 `assembled_tools` 中，同样受显式绑定、平台启停、冻结版本、模式和预算约束。Group 专用工具只在 Group 运行中生效，`select_group_members` 只在 Chat 中生效，领域执行器继续校验成员、目标范围和根 WorkItem 等条件。计划模式允许已绑定的 `update_plan`、`ask_user` 完成计划维护和澄清；这个例外不会授权业务写入。

注册升级不改写历史不可变 AgentVersion，也不为任何 Agent 自动授予新工具。需要原协调能力时，应在 Agent 草稿中选择对应工具并重新发布。Plan 模式缺少有效 `update_plan` 绑定时以 `working_plan_tool_unavailable` 明确失败，避免反复要求模型调用不可用工具。未绑定 `search_tools` 时，其他有效的已绑定工具直接呈现，避免只能授权却无法发现。

Runtime 不依据用户消息措辞、Skill 内容、模型文本、Group 开关或历史使用自动添加 Tool。自然语言不需要包含内部 Tool key；相同机器状态下，改写、翻译或标点变化不能改变有效权限。

当显式选择的 Tool 定义不存在时，Runner 拒绝构建快照；被停用、版本校验失败或受模式限制时，快照保留装配项并记录具体 `denied_capabilities`，调用返回明确的授权错误。不能抹掉原绑定或以其他工具版本替换。

## 调用边界

模型只能看到本 Turn 有效的 Platform Tool 与 Runtime Coordination Tool 定义，也只能调用该集合。Platform Tool Dispatcher 在每次调用时再次校验：

- Tool 在 Runner 快照中；
- 输入符合 Schema；
- 平台级配置可用；
- 调用满足该 Tool 自身的运行时约束。

每次 Provider 请求之前，Runtime 将冻结装配与当前平台治理相交，统一生成 Tool 的展示 schema、说明和平台操作提示；动态搜索、幂等重放和续跑恢复使用同一投影。运行中停用的 Tool 不再展示，执行边界也重新检查停用状态，包括普通 inline Tool。未绑定工具不会因为提示词、Skill、目录搜索或模型配置自动获得权限。

通用平台提示只描述权限和证据等公共规则。具体工具指令声明其所需能力，只有本轮实际呈现这些能力时才加入提示；Web 检索不再是所有 Agent 的默认步骤。旧不可变 ToolVersion 的已知平台文案可在展示层修正，原版本、绑定、合同 hash 和历史记录保持不变。负向限制和真实工具依赖不能通过删除安全条件来规避。

定时触发的运行中，已绑定 `schedule_task` 只呈现 `operation=list` 及查询参数；搜索或恢复不能重新展开修改操作。执行层继续拒绝定时运行递归修改定时任务。

当前按单个 Tool 的有效能力装配提示和 schema；未来工具组合包可以提供组合选择与指导，但必须经过同一装配、展示和执行边界，不产生隐式授权。

Runtime Coordination Tool 的执行器必须再次校验身份、模式、Membership 与目标范围，并直接写入相应领域状态和 Event。Harness 不向模型暴露用于定义完成条件或关闭 Tool 集合的控制 Tool。这些协调能力不能声明任务成功、定义必需 Tool 清单或关闭业务能力。Provider 的完整普通消息或 `end_turn` 自然结束当前 Turn，Run 状态继续由 pending Invocation、等待、取消和预算等机器事实决定。

任何模型可调用能力在授权期间都不是一次性槽位。某次 `write_file`、`execute`、`publish_artifact`、`invoke_agent` 或 `search_group_history` 成功后，Runtime 不得仅因“已经用过”就从后续 Turn 的有效集合中移除整个 Tool key。是否重复执行由具体 Invocation、参数、目标资源、因果 Action 和 operation key 判断。

`invoke_agent` 还必须执行 Agent 可发现范围和 Group 参与者集合校验，详见 [Agent 协作](agent-collaboration.md)。

`search_group_history` 只能查询当前 GroupSession 中对成员公开的 Message、ExecutionEvent 和 Artifact 索引。它必须校验当前 Agent 是 Group 成员，并排除其他 Agent 的 Instructions、私有记忆、Prompt、模型内部推理、未公开 Tool 数据和未发布 Workspace 文件。该能力需要显式绑定；同名旧注册项复用 ToolDefinition ID 并发布新 ToolVersion，旧版本保持原冻结合同。

## ToolInvocation 与执行类型

所有 Tool 调用都持久化为一个语义上的 `ToolInvocation`，底层一次或多次物理尝试记录为 `ToolAttempt[]`。

Runtime 协调工具也记录冻结版本、调用结果和幂等操作键，并消耗工具调用预算。协调请求持久化完成可结算该 ToolInvocation；`ask_user` 的 Clarification 和 Chat/Plan `invoke_agent` 的 AgentInvocation 等领域等待仍按各自恢复协议继续，幂等重放不得重复创建或丢失等待。

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

管理员可以在 Tool 列表页根据部署环境调整。后期若增加动态风险判断或垂类审批，必须先在本模块定义确定的裁决输入、优先级、失败语义和审计合同，再同步数据库、状态机与 API；不能通过非权威待办文档隐式扩展当前权限模型。

`invoke_agent` 首先要求显式 Agent 绑定和平台启用，再由 Agent Network 可发现范围、Group Membership、当前 Collaboration Policy 和预算裁决。Runtime 协调工具当前不支持 Sandbox 的执行确认恢复；其 Confirmation Policy 只允许 `never`，已有不受支持的配置会明确拒绝执行。领域审批和用户澄清继续遵守相应业务协议。

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
