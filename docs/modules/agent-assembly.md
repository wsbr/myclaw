# Agent 装配

本文是 Agent 身份、不可变发布版本、配置页面和装配约束的唯一权威来源。

## Agent 与 AgentVersion

Agent 是稳定的逻辑身份，其他 Agent、API Key、Session 和管理关系引用 `agent_id`。可执行装配保存在不可变的 AgentVersion 中：

```text
Agent
├── id
├── owner_account_id
├── visibility
├── status
├── draft_version_id
└── current_published_version_id

AgentVersion
├── id
├── agent_id
├── version
├── publication_status
└── immutable assembly fields
```

编辑发生在草稿。发布时校验完整装配并生成新的不可变 AgentVersion；已发布版本不能原地修改。再次编辑从当前草稿或已发布版本派生新草稿，不覆盖历史执行引用。

## AgentVersion 目标 Schema

```text
id
agent_id
version
publication_status
name
description
instructions
model_id
sandbox_profile_id
tool_keys[]
skill_ids[]
bound_agent_ids[]
context_policy_key
history_policy_key
memory_write_policy_key
memory_recall_policy_key
group_enabled
created_by
published_at
```

实现可以增加审计字段、租户字段和时间戳，但不能用额外字段恢复本文明确删除的隐式装配方式。

## 字段语义

- `agent_id`：稳定逻辑标识，用于长期引用和调用。
- `id + version`：不可变装配版本标识。
- `name`：面向用户展示、搜索和 Agent 发现的名称。
- `description`：独立数据库字段，说明 Agent 能承担什么任务，是 Agent Catalog 的主要发现信息。
- `instructions`：装配者唯一可编辑的 Prompt 文本区，描述领域角色、工作方法和约束。
- `model_id`：唯一模型引用。
- `sandbox_profile_id`：唯一 Sandbox Profile 引用；Session 创建时固定实际 Profile。
- `tool_keys[]`：显式选择的 Platform Tool key 集合。
- `skill_ids[]`：显式选择的 Skill 逻辑标识集合。
- `bound_agent_ids[]`：本 Agent 可发现的直接绑定 Agent。
- 四个 `*_policy_key`：分别选择平台注册的上下文、历史、记忆写入和记忆召回策略。
- `group_enabled`：是否注册到 Group 可发现网络，并让用户和当前 Agent 可以创建 Group Proposal。
- Agent 的 `status`：逻辑 Agent 是否允许创建新 Run。
- Agent 的 `visibility`：第一阶段为 `private | enterprise`，完整访问规则见 [身份与 Agent API](identity-api.md)。

Chat 与 Plan 是所有可交互 Agent 的固有执行方式，不增加 `chat_enabled` 或 `plan_enabled`。Group 会改变跨 Agent 发现范围，因此必须显式配置 `group_enabled`。

## Instructions 与平台 Prompt

`instructions` 是唯一用户编辑区。以下内容由平台生成，不应暴露为多个可自由拼接的编辑器：

- Runtime 规则；
- Tool 定义；
- Skill Catalog；
- Agent Catalog；
- 策略生成的上下文、历史和记忆；
- 当前执行上下文。

因此配置页不提供 Identity、Instructions、Context、Output Format 等多个 Prompt 编辑器。需要结构化领域指令时，装配者在一个 Instructions 文本区内组织。

其他 Agent 发现本 Agent 时只能获得 `id + name + description`，不能读取 `instructions`。

## 配置页信息架构

### 基础信息

编辑草稿中的 `name` 和 `description`，并在 Agent 级维护 `status` 与 `visibility`。

### 模型与指令

选择一个 `model_id` 和一个 `sandbox_profile_id`，编辑 `instructions`。不提供 temperature、top_p、max_tokens、宿主机路径、容器参数或请求级覆盖。

### 能力装配

选择 `tool_keys[]` 与 `skill_ids[]`。Tool 与 Skill 分开展示但不存在依赖关系，也不展示第二套“内置 Tool”选择器。

### 上下文与记忆

分别选择四个策略 key，不展示内部 Token 阈值、TTL 或存储后端参数。

### 协作与访问

维护直接绑定 Agent 和 `group_enabled`。不允许手写 Capability Manifest、Event Manifest 或 Group JSON。Plan 和 Group 都必须先向用户展示图并获得确认，不提供自动执行策略。

## 装配校验

草稿可以保存不完整状态；发布时至少校验：

- 名称、描述、指令和模型引用符合平台必填规则；
- Sandbox Profile 存在、启用且满足当前平台兼容条件；
- 所有引用对象存在且处于可选择状态；
- 不能绑定自身，绑定集合去重；
- 策略 key 类型与字段一致；
- `group_enabled` 与 Agent 的可访问状态一致。

校验不推断 Skill 是否“合理”，也不因为 Skill 文本提及某 Tool 而自动修改 `tool_keys[]`。

## 发布、停用与版本选择

- Chat 每次创建新 Run 时解析 Agent 最新已发布版本；同一 Session 不因此重建。
- Plan 和 Group 在用户确认图时固定所有参与者的 `agent_version_id`，直到本次 PlanExecution 或 GroupRun 结算。
- 图确认后发布 Agent 新版本，不改变已经确认的执行。
- Agent 停用后禁止创建新 Run；已经开始的 Run、PlanExecution 和 GroupRun 可以继续结算。
- 历史 AgentVersion 可以标记废弃并禁止新 Run 使用，但必须保留历史 Runner 引用。
- Agent 相互绑定始终绑定稳定 `agent_id`；实际执行时再按当前模式规则解析版本。

Skill 使用独立的热更新规则：AgentVersion 固定 `skill_id` 装配关系，Runner 启动时解析该 Skill 当前有效版本并固定 checksum。完整定义见 [Skill](skill-system.md)。

## 运行时解析

Runtime 先根据 Chat Run 或已确认 Plan / Group 的版本规则取得 AgentVersion，再解析引用。解析结果交给 [Runtime 执行](runtime-execution.md) 固定为 Runner 快照。正在运行的 Runner 不切换 AgentVersion。

`sandbox_profile_id` 是例外：它只在创建 SessionSandbox 时解析。既有 Session 后续 Runner 继续使用 Session 固定的 Sandbox，Agent 修改只影响新 Session。
