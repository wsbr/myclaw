# Platform Tool

本文是 Tool 注册、治理、装配和有效权限的唯一权威来源。

## 定义

Platform Tool 是由 MyClaw 后端代码实现、注册并通过 Runtime 调度的动作能力。平台只有一个 Tool Registry，不区分 Built-in Tool 和数据库自定义 Tool 两套体系。

`read_file`、`execute`、`invoke_agent`、`search_group_history` 均是普通 Platform Tool。它们可能由核心代码提供，但不会因此被隐式加入 Agent。

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
- 在 `never | always` 之间切换 `confirmation_policy`；
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
                  ∩ current_mode_and_phase_scope
                  ∩ frozen_plan_or_group_scope
```

Runtime 不依据用户消息措辞、Skill 内容、模型文本、Group 开关或历史使用自动添加 Tool。自然语言不需要包含内部 Tool key；相同机器状态下，改写、翻译或标点变化不能改变有效权限。

当显式选择的 Tool 不存在或被停用时，Runner 应明确失败或按平台定义拒绝启动，不能静默删除 Tool 后继续造成能力漂移。

## 调用边界

模型只能看到有效 Tool 集合的定义，也只能调用该集合。Tool Dispatcher 在每次调用时再次校验：

- Tool 在 Runner 快照中；
- 输入符合 Schema；
- 平台级配置可用；
- 调用满足该 Tool 自身的运行时约束。

Harness 可以额外暴露不进入 Platform Tool Registry 的控制 Tool，例如用于提交封闭完成信封的 `finalize_response`。控制 Tool 不能访问业务系统、不能扩大 AgentVersion 权限，并且必须在 Runner 诊断中与业务 Tool 分开记录。

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

平台重试只处理同一个 ToolInvocation、完全相同参数和幂等键的瞬时技术失败。Agent 看不到中间 ToolAttempt，只看到最终状态和结果。

```text
ToolInvocation
├── ToolAttempt 1：瞬时失败
├── ToolAttempt 2：瞬时失败
└── ToolAttempt 3：成功
```

是否可重试由 Tool 平台实现决定。有副作用且无法确认远端是否成功时进入 `outcome_unknown`，不能盲目重放。异步回调按 `invocation_id` 去重。

## 轻量 Tool Confirmation

第一阶段由 Platform Tool Registry 提供静态策略：

```text
confirmation_policy: never | always
```

- `never`：新的 ToolInvocation 直接进入可执行状态；
- `always`：每一个新的 ToolInvocation 都需要原始用户确认。

策略是平台级实时治理配置，不复制到 AgentVersion，也不允许 Agent 级覆盖。第一阶段不根据参数动态判断风险，不定义通用风险等级、金额策略、组织审批、批量预授权或复杂 Governance Profile。

需要确认时：

```text
ToolInvocation = waiting_confirmation
→ 用户查看 Tool 与精确参数
→ confirm | reject
→ 确认后执行同一个 ToolInvocation
```

确认记录绑定 `tool_key + arguments_hash + execution_principal`；参数变化必须重新确认。已确认调用的同参数平台重试不重复确认。等待确认不占用 Runner，拒绝以 `rejected` 返回 Agent。

平台前端只是默认客户端。外部服务可以通过标准 API 查询 required action 并提交确认或拒绝。Instructions 或 Skill 可以要求 Agent 进行业务上的对话确认，但不能替代 Tool 执行边界的 Confirmation。

只有 ToolInvocation 原始执行 `userId` 对应的用户可以确认或拒绝。Builder 和管理员可以停用 Tool 或取消异常执行，但不能代替用户批准业务操作。

等待确认时，Runner 保存 Checkpoint 后结束，WorkItem 进入可恢复等待。第一阶段默认不自动超时拒绝；用户可以稍后确认、拒绝或取消执行。

### 配置变更的即时语义

Runtime 在底层执行前必须重新读取 Tool 当前启停和 Confirmation Policy，采用以下安全非对称规则：

- `never → always`：尚未开始底层执行的 ToolInvocation 转为 `waiting_confirmation`；
- `always → never`：新的 ToolInvocation 不再等待，但既有 `waiting_confirmation` 不会被自动批准；
- Tool 停用：拒绝新调用，尚未开始执行的调用以 `tool_disabled` 结算；
- 已经进入底层执行的调用不承诺强制中断或回滚；
- 重新启用 Tool 不复活已经失败、拒绝或取消的 ToolInvocation。

首批注册默认值：

| Tool 类型 | 默认策略 |
| --- | --- |
| `read_file`、`invoke_agent`、只读查询 | `never` |
| `write_file`、`execute`、外部副作用 Tool | `always` |

管理员可以在 Tool 列表页根据部署环境调整。后期动态风险判断和垂类审批边界保留在 [待确认旧设计](../pending/legacy-designs-to-review.md)。

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

当前阶段平台不判断装配组合是否合理，由装配者负责选择。
