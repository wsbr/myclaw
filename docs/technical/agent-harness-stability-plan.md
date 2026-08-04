# 三模式 Agent Harness 稳定性优化方案

## 1. 文档目的与结论

本文定义 MyClaw Chat、Plan、Group 三种模式共用的 Agent Harness 重构方案。这里的 Harness 指从 Runner 获得执行权开始，到装配解析、能力裁决、Prompt 构造、模型调用、Tool/Agent 调用、等待恢复、结果校验、结算和观测结束的完整运行边界。

本方案不改变产品已经确认的三模式语义，而是修复当前实现对这些语义的偏离。产品和技术依据为：

- [设计哲学与产品原则](../DESIGN_PHILOSOPHY.md)：Agent 负责语义判断，平台负责授权、可靠执行和失败暴露；
- [第一阶段交付边界](../modules/phase-one-scope.md)：Chat、Plan、Group 必须共用执行原语并同时闭环；
- [Agent 装配](../modules/agent-assembly.md) 与 [Platform Tool](../modules/tool-system.md)：Tool 权限来自不可变 AgentVersion 的显式装配，而不是用户输入文本；
- [Runtime 执行](../modules/runtime-execution.md)：模型只能看到有效 Tool 集合，Tool Dispatcher 负责 Schema、身份和模式校验；
- [Agent 协作](../modules/agent-collaboration.md)、[Plan 模式](../modules/plan-mode.md) 和 [Group 模式](../modules/group-mode.md)：三种模式共享原语，但使用不同协作拓扑和冻结边界；
- [执行事件与可观测性](../modules/observability.md)：业务事实、恢复状态和非权威 Trace 必须分层且可以互相对账。

本次 UAT 证明当前问题不是单一 Provider 波动，而是 Harness 的系统性失配：

1. 补丁把“显式装配授权”错误收窄成“用户文本必须包含内部 Tool key”，形成 `text-as-capability`；
2. 模型协议、业务状态机和结果验收之间没有统一的 Model Turn 契约；
3. Proposal 依赖 Prompt 要求 JSON，没有使用可协商的结构化输出协议；
4. Runner 快照记录装配前事实，却没有记录最终暴露给模型的能力集合；
5. 自动化测试复用了内部口令，验证了补丁实现，却没有验证自然业务交互；
6. 状态与 Span 的成功语义不一致，使观测汇总掩盖结构校验失败。

因此禁止用以下方式继续修补：

- 为更多自然语言补充正则、关键词或中英文同义词；
- 在 Agent Instructions 中写死 `You must call <tool>`；
- 把模型输出的 XML/JSON 文本直接当作已授权 Tool 调用执行；
- 通过增加无界重试掩盖结构化输出失败；
- 只调整 UAT Agent 配置或模型 Prompt 就宣称 Runtime 缺陷关闭；
- 修改测试输入去迎合内部 Tool key，而不保留自然业务表达。

目标方案称为 **Harness V2**。核心决策是：

> 权限由可信机器状态决定，语义动作由模型在已授权能力内选择，执行结果由状态机和 Effect Ledger 证明，稳定性由 Model Turn、错误分类、重试预算和可对账观测保证。

## 2. 当前失效机制

### 2.1 `text-as-capability` 违反产品授权模型

权威产品定义中的有效 Tool 集合是：

```text
effective_tools = resolve_enabled(agent_version.tool_keys)
                  ∩ principal_permissions
                  ∩ mode_scope
                  ∩ current_governance
```

当前补丁链 `ce808c2`、`b7e6d37`、`d1434ef` 又增加了用户/任务文本匹配：

```text
effective_tools = effective_tools
                  ∩ regex_match("call <tool_key>")
```

这一额外交集不在产品定义中，并产生四类后果：

- 普通中文业务表达无法获得 Tool Definition，模型没有机会做结构化调用；
- 标点、语言、大小写和转述方式变成权限条件，中文全角标点也可能导致授权失败；
- Agent Instructions 明确要求“缺信息先澄清、需要报告则发布”，仍不能影响 Runtime 的文本正则；
- Runner 快照显示 Tool 已装配，但实际 Provider 请求的 Tool 集合为空，审计人员被误导。

安全目标本来是“Prompt、Skill 或被调用方不能扩大装配权限”，正确实现应校验机器态交集，而不是要求用户掌握内部 key。

### 2.2 Model Turn 没有成为一等执行对象

当前模型调用把多个不同结果压缩为“文本或 tool_calls”：

- Provider 传输失败；
- 流在首个 payload 前中断；
- 流在部分文本后中断；
- Provider 返回普通文本；
- Provider 返回 Tool Calls；
- Provider 返回 Tool 形状的普通文本；
- Provider 返回不可解析的结构化 Proposal；
- Schema 合法但业务图非法。

这些结果具有完全不同的重试和结算语义，却没有独立持久化的 ModelTurn/ProviderAttempt。失败时只有 Runner error 或最终 Message，无法稳定回答：请求实际暴露了哪些 Tool、采用什么响应契约、收到多少 payload、为什么允许重试、结构校验在哪一层失败。

### 2.3 Proposal 是“提示词 JSON”，不是结构化协议

Plan Proposal 和 Group Formation 当前通过 System Prompt 要求严格 JSON，然后对完整文本直接 `json.loads`。Provider 请求没有声明 `response_format/json_schema`，修复 Runner 也只得到泛化的“返回纯 JSON”提示，没有获得：

- 上一次输出的安全引用；
- 精确 JSON Pointer/字段错误；
- 图约束错误；
- Provider 是否支持原生 structured output；
- 输出被截断还是被 Markdown/思考前缀污染。

UAT 中 30 个 Plan Proposal 有 29 个首轮 `JSONDecodeError`，说明失败是协议级常态而非偶发异常。仅增加一次重试使请求量近乎翻倍，并在两个串行 Runtime Worker 下放大成数十分钟排队。

### 2.4 生命周期成功与业务闭环成功混淆

自然 Chat 的三个 Run 都是 `completed + succeeded`，但没有 Clarification、ToolInvocation 或 Artifact。当前结算只证明模型生成了文本，没有验证文本声称的动作是否真实发生，也没有阻止 `<tool_call>`、不存在的 Tool 或“文件已保存”等伪执行内容成为成功消息。

权威设计已经区分 `lifecycle` 与 `outcome`，Harness 必须真正使用这个区分：

- `completed`：本轮已经收敛，不再运行；
- `succeeded`：本轮响应契约与必要执行效果均满足；
- `partially_succeeded`：交付了部分可验证结果并明确缺口；
- `failed`：模型协议、能力执行或业务交付契约未满足。

### 2.5 观测数据无法解释实际模型调用

当前数据有以下不一致：

- Runner Tool Snapshot 保存装配 Tool，不保存最终 `exposed_tools` 及过滤原因；
- 31 个结构化失败 Trace 的 `runner_execution` Span 为 `ok`；
- lost Runner 留下一条长期 `running` Trace；
- Span 预留 `input_summary/output_summary`，实际没有填充；
- OTel Collector 只有 debug exporter，没有可查询的持久 Trace 后端；
- `sync_skill` Outbox 存在长期 pending，证明 Topic 声明与消费者覆盖没有统一门禁。

这些问题不一定改变权威业务状态，但会让故障定位、容量评估和发布判断失真。

## 3. Harness V2 设计原则

### 3.1 权限、语义和执行分层

必须严格区分三个概念：

| 概念 | 决定者 | 含义 |
| --- | --- | --- |
| Assembled Capability | AgentVersion | Builder 明确选择的 Tool/Skill/Agent |
| Effective Capability | Runtime | 装配、身份、模式、冻结图、治理状态的机器态交集 |
| Selected Action | Model | 模型在 Effective Capability 中基于自然语义选择的调用 |

用户输入、Prompt、Skill 文本和模型文本都不能扩大 Effective Capability；但用户不需要说出内部 Tool key 才能使用已经授权的能力。

### 3.2 三模式共享内核，差异进入 Mode Driver

共享内核负责：

- ExecutionPrincipal 与版本固定；
- Capability Envelope；
- Prompt Package；
- Model Turn 与 Provider Attempt；
- Tool/Agent 调用持久化；
- Checkpoint、恢复、预算和错误分类；
- Effect Ledger、结算和观测。

模式差异只能由 `ChatModeDriver`、`PlanModeDriver`、`GroupModeDriver` 提供，不允许继续散落为一个 Worker 中彼此覆盖的条件分支。

### 3.3 不进行隐式能力补齐和模型 fallback

Harness V2 不改变以下边界：

- 未装配的 Tool 不出现；
- Skill 不增加 Tool；
- Group 启用不自动增加 `invoke_agent`；
- 不切换到其他模型；
- 不修改冻结的 Plan 图或 Group 参与者；
- Tool 当前停用或需要确认时继续执行平台注册治理；
- Provider 不支持所需协议时明确拒绝或使用注册时声明的兼容路径，不暗中降级。

### 3.4 所有重试都有边界和证据

每次重试必须能回答：错误类别、是否已产生 payload、是否已提交副作用、重试预算、退避时间、父 Attempt 和幂等边界。任何无法回答这些问题的重试都不得执行。

### 3.5 “稳定性保证”的边界

Harness 可以确定性保证权限不越界、状态不重复推进、副作用不被文本伪造、失败不被错误标记为成功、重试不越过幂等边界以及每次调用可解释。Harness 不能数学保证外部 Provider 永不超时，也不能保证概率模型对所有新业务表达都做出正确领域判断。

因此验收分为两类：

- **确定性正确性**：安全、状态、幂等、冻结、恢复、效果真实性和观测一致性，要求 100% 通过；
- **模型质量与可用性**：自然意图到 Tool、结构化输出、延迟和修复率，在固定模型、固定样本、固定并发上用统计门禁约束。

不得用“模型有概率”豁免确定性错误，也不得把一次自然场景成功包装成普遍稳定性保证。

## 4. 目标架构

```text
RunnerCoordinator
  ├── ExecutionSnapshotResolver
  ├── ModeDriver
  │    ├── ChatModeDriver
  │    ├── PlanModeDriver
  │    └── GroupModeDriver
  ├── CapabilityResolver
  ├── PromptBuilder
  ├── ModelTurnCoordinator
  │    ├── ProviderCapabilityNegotiator
  │    ├── ProviderAdapter
  │    ├── StreamAccumulator
  │    ├── ResponseValidator
  │    └── RetryPolicy
  ├── ActionDispatcher
  │    ├── ToolDispatcher
  │    ├── AgentInvocationGateway
  │    └── ClarificationGateway
  ├── EffectLedger
  ├── SettlementService
  └── Event/Checkpoint/Trace Writers
```

`RuntimeWorker` 只负责领取命令、取得 Runner lease、调用 Coordinator 和处理 fencing；它不再同时承担 Prompt、Provider、Tool、Plan、Group、结算和恢复的全部业务逻辑。

### 4.1 Mode Driver 接口

```python
class ModeDriver(Protocol):
    def phase(self, execution) -> ModePhase: ...
    def capability_policy(self, execution) -> ModeCapabilityPolicy: ...
    def response_contract(self, execution) -> ResponseContract: ...
    def prompt_sections(self, execution) -> list[PromptSection]: ...
    async def apply_model_result(self, result, execution) -> Transition: ...
    async def validate_completion(self, execution, ledger) -> CompletionDecision: ...
```

Mode Driver 输出声明，不直接调用 Provider 或修改任意模式外对象。所有状态迁移仍由 Settlement/Domain Service 在短事务中完成。

### 4.2 Capability Envelope

每个 Runner 在首次模型调用前生成不可变 `CapabilityEnvelope`：

```json
{
  "runner_id": "...",
  "agent_version_id": "...",
  "mode": "chat|plan|group",
  "phase": "...",
  "principal_hash": "...",
  "assembled_tools": ["read_file", "write_file"],
  "effective_tools": ["read_file", "write_file"],
  "denied_tools": [
    {"tool_key": "invoke_agent", "reason": "chat_child_star_topology"}
  ],
  "callable_agents": ["..."],
  "group_node_ids": ["..."],
  "governance_versions": {"write_file": 3},
  "hash": "..."
}
```

裁决顺序固定为：

1. AgentVersion 是否显式装配；
2. Tool 当前是否存在和启用；
3. ExecutionPrincipal 是否允许；
4. 当前 Mode/Phase 是否允许；
5. Plan/Group 冻结范围是否允许；
6. 调用时重新检查 Tool 当前治理配置。

`effective_tools` 原样传给 Model Adapter。任何用户字符串、Prompt 片段或正则都不得参与该集合的计算。

Tool Dispatcher 收到模型调用后再次使用同一 Envelope 校验。模型调用未知或未授权 Tool 时，创建安全的拒绝记录/事件并以结构化 Tool Error 返回模型；不得执行，也不得把整个 Runner 无差别崩溃成内部错误。

### 4.3 Prompt Package

PromptBuilder 输出版本化 `PromptPackage`，每段至少记录：

```text
section_key
source_type / source_id / source_version
checksum
token_estimate
visibility
redaction_policy
```

固定顺序继续遵循 Runtime 权威文档。新增约束：

- Tool Definitions 必须与 Capability Envelope 的 `effective_tools` 完全一致；
- Agent Catalog 必须与 `callable_agents` 完全一致；
- 恢复 Runner 必须引用上一个安全 Checkpoint 的标准化 Model Turn，而不是重建未经证明的隐式上下文；
- Prompt Package 的 hash、Toolset hash 和 Response Contract key 必须进入 Model Turn；
- Prompt 不再描述“只有用户说出某句话才可调用 Tool”。

### 4.4 Model Turn 与 Provider Attempt

新增语义对象 `ModelTurn`，表示一次需要得到完整模型决策的逻辑调用；一次 ModelTurn 可以因安全的传输重试包含多个 `ProviderAttempt`。

```text
ModelTurn
├── purpose: conversation | tool_followup | structured_proposal | settlement
├── prompt_package_hash
├── capability_envelope_hash
├── response_contract_key/version
├── status
├── selected_result_type
└── ProviderAttempt[]
```

ProviderAttempt 至少记录：

```text
provider / model / parameter_version
request_id / provider_response_id
started_at / first_payload_at / ended_at
payload_state: none | partial_text | partial_tool | complete
finish_reason
input/output tokens
transport/protocol error category
retry_parent_attempt_id
redacted output digest and bounded diagnostic excerpt
```

ModelTurn 标准结果是封闭联合类型：

```text
AssistantText
ToolCalls[]
StructuredValue
TransportFailure
ProtocolFailure
ValidationFailure
Cancelled
```

Provider Adapter 只做协议转换，不做模式判断、业务结算或 Tool 授权。

### 4.5 Response Contract

每个 Mode/Phase 必须显式选择响应契约：

| Contract | 允许结果 | 典型阶段 |
| --- | --- | --- |
| `conversation.v1` | 文本或 Tool Calls | Chat、Group 节点工作、普通 Plan Owner 工作 |
| `tool_required.v1` | 至少一个指定集合内 Tool Call | Plan Owner 协调已就绪子节点等机器已知动作 |
| `structured.plan_proposal.v1` | Plan JSON Schema | Plan Proposal |
| `structured.group_formation.v1` | Group Formation JSON Schema | Group Formation |
| `settlement.v1` | 无 Tool 的最终汇总 | Plan/Group 结算 |

`tool_required` 只在平台已经通过冻结图/状态机确定“本阶段必须调用某类能力”时使用，不从自然语言关键词推断。例如 Plan Scheduler 已经确定某个子 Agent 节点就绪，此协调阶段必须得到合法 `invoke_agent`，模型返回普通文本不能把节点标记成功。

### 4.6 Effect Ledger 与交付真实性

每个 Work Item 维护只引用权威领域记录的 Effect Ledger：

```text
clarification_ids[]
tool_invocation_ids[]
agent_invocation_ids[]
workspace_write_ids[]
artifact_ids[]
rejected_action_ids[]
```

Harness 在提交最终 Message 前执行一致性校验：

- 文本含 `<tool_call>`、`<function=...>` 或已注册 Tool 的伪调用格式，但本轮没有结构化 Tool Call：记为 `model_protocol_violation`，不直接成功；
- 文本声称“已保存、已发布、已调用专家、已查询记录”，但 Ledger 没有对应效果：禁止 `succeeded`，进行一次受限纠正或以明确失败/部分成功结算；
- Artifact 只能由已发布 Artifact 记录挂载，不能从文本推断；
- Tool/Agent 的失败和拒绝必须在最终交付中可见，不得被模型文本覆盖。

检测器只阻止伪执行和错误成功，不把文本解析为可执行调用，从而避免新的注入通道。

### 4.7 Completion Contract

Response Contract 约束“模型必须返回什么形状”，Completion Contract 约束“本 Work Item 满足什么条件才可结算”。它只能来自可信机器状态：

```text
required_effect_types[]
required_agent/node results[]
allowed_terminal_outcomes[]
acceptance_criteria_refs[]
```

- Plan 来自冻结节点、依赖和 acceptance criteria；
- Group 初始工作来自已确认 Formation revision，后续定向工作来自已持久化 Work Item；
- Chat 默认只强制协议完整和效果真实性。若 API、产品流程或验收场景明确登记 Artifact/Tool 等必要效果，则一并校验；Harness 不用关键词正则把任意用户句子提升成权限或不可撤销业务合同。

Completion Contract 与 Effect Ledger 对账后，由 SettlementService 决定 outcome。模型只负责说明结果和缺口，不能自行声明合同已经满足。

## 5. Provider 能力与模型调用稳定性

### 5.1 Model Capability Profile

Model Registry 的 capabilities 扩展为可验证配置：

```text
streaming
native_tool_calling
parallel_tool_calls
structured_output: json_schema | json_object | none
usage_in_stream
provider_request_id
max_context_tokens
max_output_tokens
```

模型连接测试不能只测普通文本，还必须分别执行：

1. 流式中文文本；
2. 单 Tool Call；
3. 多 Tool Call 及分片参数；
4. Tool result 后续轮次；
5. Plan/Group 对应 JSON Schema；
6. 零输出、超时和 429/5xx 错误映射。

测试结果与 capability/adapter version 一起保存。Agent 发布时校验所选模型是否满足该 Agent 的模式和 Tool 需求；不满足时阻止发布或明确限制模式，不能到 Runtime 才随机失败。

### 5.2 结构化输出策略

优先级固定为：

1. Provider 原生 `json_schema`；
2. Provider `json_object` + 服务端 Schema 校验；
3. 明确注册的兼容模式：非流式、低温、严格边界提取 + Schema 校验；
4. Provider 不具备稳定结构输出时，不允许承担 Proposal 生成。

Plan/Group Schema 除字段类型外，还必须校验关系化不变量：Agent 可访问、Owner/入口唯一、node/participant key 唯一、DAG 无环、依赖合法、Group 至少一个初始 Work Item、参与者满足 `group_enabled`。

修复尝试必须携带机器生成的最小错误列表，例如：

```json
{
  "error_code": "structured_validation_failed",
  "errors": [
    {"path": "/nodes/1/assigned_agent_id", "code": "agent_not_in_catalog"}
  ],
  "previous_output_digest": "..."
}
```

原始输出只在受限诊断存储中保存脱敏摘要/哈希；不能进入用户事件或普通 Prompt 全文。一次原始生成加一次修复为默认硬上限。超过上限明确失败，不继续排队风暴。

### 5.3 流式累积器

统一 StreamAccumulator 必须覆盖：

- content 与 tool call 分片交错；
- Tool Call `id/name/arguments` 分别到达；
- 多 Tool Call index；
- usage-only 尾帧；
- 空 choices；
- Provider finish reason 差异；
- UTF-8 分片边界；
- 参数 JSON 不完整；
- 首 payload 前和首 payload 后断流。

只有完整 Turn 才能进入 Checkpoint。部分文本可以实时展示，但 UI 必须标识为 provisional；Turn 未完成时不得落为最终 Message。

### 5.4 错误分类与重试矩阵

| 错误 | 是否自动重试 | 执行边界 |
| --- | --- | --- |
| 连接失败/超时且 payload=none | 是，最多 1 次，指数退避+jitter | 同一 ModelTurn 新 ProviderAttempt |
| 429/5xx 且 payload=none | 是，遵循 Retry-After，总预算内 | 释放 Worker，延迟 Outbox/新 Runner |
| 部分文本后断流 | 默认否 | `model_stream_interrupted_after_payload`，防止重复展示/动作 |
| 部分 Tool Call 后断流 | 否 | 未形成完整调用，不 dispatch；明确协议失败 |
| Tool 参数 JSON 非法 | 可进行 1 次模型纠正 | 不创建业务 ToolAttempt，不执行副作用 |
| Structured Schema 失败 | 1 次有错误上下文的修复 | 新 Runner/ModelTurn，关联父 Turn |
| Prompt 超预算 | 否 | 只允许既定 Context/History Policy 处理，否则 `prompt_too_large` |
| Tool/Agent 业务失败 | 否 | 作为结构化结果返回模型，不属于模型重试 |
| Provider 凭证/模型停用 | 否 | 配置错误立即失败，不 fallback |

同时引入每 Run 的统一预算：

```text
max_model_turns
max_provider_attempts
max_tool_calls
max_structured_repairs
max_wall_time
max_input_tokens / max_output_tokens
```

预算耗尽是明确业务可见错误，不能表现为永久 `running`。

### 5.5 调度、隔离与背压

当前两个 Worker 在执行模型请求时串行占用消费循环，30 个 Proposal 且几乎全部修复会形成严重队头阻塞。Harness V2 至少划分四类执行 lane：

```text
interactive_chat
graph_proposal
plan_group_execution
maintenance
```

要求：

- 每个 lane 有独立并发上限和队列等待指标；
- Provider 级 semaphore 控制真实并发，避免 429 风暴；
- 交互 Chat 保留容量，不被 Proposal 压测或 Memory 摘要饿死；
- 退避任务释放 Worker，不在进程内 sleep 占槽；
- 同一 Session/Work Item 的 fencing 不变；
- 调度容量不改变业务优先级事实，只影响何时取得执行权。

## 6. 三模式 Harness 设计

### 6.1 Chat Harness

#### 阶段

```text
input
→ conversation/tool loop
→ optional waiting_user|waiting_tool|waiting_agent|waiting_resource
→ resumed conversation/tool loop
→ completion validation
→ settlement
```

#### 能力规则

- 根 Agent：使用其已装配、启用且主体允许的 Tool；自然语言不参与授权；
- `invoke_agent`：仅当已装配，目标仅限直接绑定、可访问、已发布 Agent；
- Chat 子 Agent：使用目标 Agent 自己的有效 Tool，但强制移除 `invoke_agent`；不能直接创建面向用户 Clarification，需返回结构化 `needs_clarification` 给根 Agent；
- Tool Confirmation 继续按调用前的平台注册当前值执行。

#### 稳定性门

- 模型返回 Tool 形状文本时不得完成为成功；
- Tool result、Clarification answer 和 Agent result 恢复后必须通过 Checkpoint 只消费一次；
- 最终 Message 附件只来自 Effect Ledger；
- Completion Contract 已登记必须交付文件而 Ledger 没有 Artifact 时，Harness 至少明确“未生成”并以 partial/failed 结算，不能声称完成。

#### 必测自然场景

用户全程不出现任何 Tool key，用中文提出“缺信息就问、读取规则、计算金额、保存并发布报告”。必须形成真实 Clarification、ToolInvocation、Artifact 和可下载内容。

### 6.2 Plan Harness

Plan 拆分为三个彼此独立的 Response Contract。

#### Proposal

- 使用 `structured.plan_proposal.v1`；
- 不暴露 Tool；
- 使用 Model Capability Profile 协商 JSON Schema；
- Schema 和图约束全部通过才进入 `awaiting_confirmation`；
- 校验失败只允许一次有依据的修复；
- Proposal 失败不创建半成品关系图或重复 Work Item。

#### Frozen execution

- Scheduler 只根据冻结 DAG 计算 ready；
- Owner 自身节点使用 `conversation.v1` 和 Owner 有效 Tool；
- 子 Agent 节点就绪时创建 Owner coordination Work Item，使用 `tool_required.v1(invoke_agent)`；普通文本不能把协调节点标记成功；
- `invoke_agent` 的 agent_id/node_key 必须来自 Capability Envelope 与冻结 Plan；
- 子 Agent 使用固定 AgentVersion、自身有效 Tool、空 Agent Catalog；
- 节点失败按 success/settled 依赖传播，不通过模型重试伪装成功。

#### Settlement

- 使用 `settlement.v1`，Tool 集合为空；
- 输入只包含全部节点的权威状态、结果摘要、错误和 Artifact 引用；
- Harness 先计算 outcome，模型只负责语义汇总，不能反向修改状态；
- 汇总必须列出 failed/blocked/cancelled/incomplete 项。

#### 稳定性门

- 同一 Plan 最多一个 `draft_ready`；
- 冻结后任何 ModelTurn 不得改变图或 AgentVersion；
- 30 个并发 Proposal 必须按容量门禁完成，不通过无限排队获得表面成功；
- Proposal、执行、结算分别统计延迟和成功率，禁止只汇总成一个 Run 成功率。

### 6.3 Group Harness

#### Formation

- 使用 `structured.group_formation.v1`；
- 只把 `group_enabled + published + accessible` Catalog 暴露给模型；
- 不暴露 Tool；
- 服务端验证入口唯一、参与者/初始 Work Item/建议边合法；
- 用户确认前不创建正式 Group 节点或业务 Work Item。

#### Group Work

- 节点使用自身 AgentVersion 的有效 Tool；
- `request_clarification` 由 Mode Policy 允许，并创建定向持久等待；不需要用户说出 Tool key；
- `invoke_agent` 仅当 Agent 装配且目标在本 GroupRun 可用固定节点中；
- `search_group_history` 仅当 Agent 装配且当前 Runner 属于 Group，查询继续使用统一可见投影；
- 用户定向消息只唤醒接收节点，不能因共享 Session 把全部记录注入 Prompt；
- Group 内被调用 Agent 可以使用自身有效 Tool并继续图内协作，但 delegation chain、环检测和深度预算必须进入 Capability Envelope。

#### 图外边界验证

自然业务场景与安全边界场景分开：

- 自然场景中，模型看到的 Catalog 不含图外 Agent，直接解释“无权限”是正确语义结果，不要求模型为了产生拒绝事件而故意攻击 Runtime；
- 安全测试通过 Mock/协议注入一个结构化图外 `invoke_agent` 调用，必须产生可审计拒绝且不创建目标 Work Item；
- 两者都必须通过，不能用模型的一次文字拒绝替代 Runtime 授权测试，也不能要求自然用户输入内部口令。

#### Settlement

- 全部相关 Work Item all-settled 后才启动；
- outcome 由权威状态计算，Owner 只负责汇总；
- 终态 GroupRun 不恢复，新追问创建新 GroupRun 并重新固定当前可用版本。

## 7. 数据模型与接口调整

### 7.1 建议新增表

#### `model_turn`

```text
id, runner_id, step_no, purpose
prompt_package_hash, capability_envelope_hash
response_contract_key, response_contract_version
status, result_type, finish_reason
input_tokens, output_tokens
output_digest, diagnostic_excerpt
error_category, error_code
started_at, completed_at
```

#### `provider_attempt`

```text
id, model_turn_id, attempt_no
provider, model_identifier, parameter_version
request_id, provider_response_id
payload_state, first_payload_at
status, error_category, error_code
retry_parent_attempt_id
started_at, ended_at
```

#### `runner_capability_snapshot`

```text
runner_id, platform_tool_id
assembled, registry_enabled, principal_allowed, mode_allowed
exposed_to_model, decision_code
implementation_version, config_version
```

可保留现有 `runner_tool_snapshot` 作为版本快照，但不得再单独承担“实际可调用 Tool”解释职责。

### 7.2 Checkpoint 扩展

Checkpoint 至少增加：

```text
last_completed_model_turn_id
conversation_turn_cursor
consumed_tool_invocation_ids[]
consumed_agent_invocation_ids[]
pending_required_action_id
effect_ledger_version
retry_budget_snapshot
```

恢复只消费权威 Invocation 结果，不重放未完成 Provider 流，也不从 OTel Trace 猜测执行状态。

### 7.3 错误码体系

统一错误类别：

```text
capability_denied
model_transport_before_payload
model_stream_interrupted_after_payload
model_rate_limited
model_protocol_invalid_tool_call
model_protocol_tool_shaped_text
model_structured_output_invalid
model_structured_output_unsupported
prompt_too_large
model_budget_exhausted
runner_lost_without_checkpoint
settlement_contract_failed
```

错误码必须在 Run/WorkItem/Runner/ModelTurn/Span 中采用一致映射；用户事件只暴露安全、可行动的子集。

## 8. 可观测性与数据归集

### 8.1 必须能回答的问题

每个 Runner/ModelTurn 必须可以查询：

- 实际 AgentVersion、Model 和参数版本；
- assembled/effective/denied/exposed Tool 及 reason code；
- Mode、Phase 和 Response Contract；
- Prompt 各段来源、hash 和 token；
- ProviderAttempt 次数、首 token、总延迟和 payload 状态；
- 模型返回文本、Tool Calls 还是 Structured Value；
- 结构校验失败路径和修复关系；
- Tool/Agent/Clarification 的权威效果；
- 最终 lifecycle/outcome 为什么成立。

### 8.2 指标

按 `mode/phase/provider/model/agent_version/response_contract` 聚合：

```text
runner_queue_wait_seconds
model_first_payload_seconds
model_turn_duration_seconds
model_turn_total / success / failure
provider_attempt_total / retry_total
tool_exposed_count / tool_call_count / tool_rejected_count
tool_shaped_text_violation_total
structured_validation_failure_total
structured_repair_total / repair_success_total
run_completion_total by outcome
trace_state_mismatch_total
stale_running_trace_total
outbox_oldest_pending_seconds by topic/action
```

### 8.3 Trace 状态一致性

- `runner_execution` Span 的最终 status 从 Runner 终态派生，不能只看 Python 是否抛异常；
- Runner 被标记 lost/cancelled 时，由恢复/控制路径关闭旧 Trace；
- failed Runner 必须至少有一个 error Span 或显式 `terminal_status=failed` 属性；
- Trace 与领域状态定期对账，不一致产生平台告警但不修改领域事实；
- `input_summary/output_summary` 只保存脱敏摘要、Schema 错误和 hash，不保存凭证、完整 Prompt 或模型私有推理。

### 8.4 归集后端

UAT/生产不能只使用 debug exporter。至少配置一个可查询、带保留期的 OTLP Trace 后端；MySQL Runner Trace 继续保留为应用内脱敏诊断索引，两者通过 trace_id 对账。

Collector 必须同时具备：

- memory limiter、batch、持久后端 exporter；
- exporter 失败重试和自身健康指标；
- 按环境/service.name/release 区分；
- 禁止把凭证和未脱敏正文写入 attributes。

## 9. 测试与发布门禁

### 9.1 四层测试

#### A. 不变量单测

- 用户文本、语言和标点变化不改变 Effective Capability；
- 未装配 Tool 永远不暴露；
- Chat 子 Agent 无 `invoke_agent`；
- Plan/Group 固定范围不能扩大；
- Tool 当前停用/Confirmation Policy 正确即时生效；
- 状态迁移、Effect Ledger 和 Trace 终态一致。

#### B. Provider Contract Test

每个 Adapter 使用录制/模拟协议帧覆盖完整 StreamAccumulator 矩阵，包括多 Tool 分片、usage-only 帧、非法参数、首 payload 前后断流、429/5xx、JSON Schema 和 Provider finish reason。

#### C. 状态机与故障注入

在 ModelTurn、ToolInvocation、AgentInvocation、Checkpoint、Outbox claim/ack、Runner lease 等边界重启 Worker，断言唯一推进者、无重复副作用、无终态复活、Trace 可关闭。

#### D. 产品自然语言 E2E

测试输入不得出现内部 Tool key。三模式分别使用真实中文业务任务，断言领域对象和事件，而不是断言模型恰好输出某句话。

### 9.2 安全测试与自然交互测试分离

安全边界通过构造结构化恶意 Model Result 验证，例如未装配 Tool、图外 Agent、越权文件路径和其他节点 direct 记录。自然交互测试验证模型能在授权集合中根据业务意图选择正确能力。两类测试不能互相替代。

### 9.3 硬正确性门禁

- 自然 Chat Artifact 场景 10/10 闭环，0 次 Tool-shaped text 被当成功；
- Plan Proposal 固定真实模型 30/30 最终 draft-ready，0 重复图/Work Item；
- Group 自然澄清、图内调用和历史查询各 10/10 形成真实领域对象；
- 结构化恶意图外调用 100% 被 Runtime 拒绝；
- Worker 故障注入后业务副作用重复数为 0；
- failed Runner 与 ok terminal Span 不一致数为 0；
- stale running Trace 和超过阈值的未消费 Outbox 为 0；
- 任何密钥、Authorization Header 和未脱敏 Prompt 泄露为 0。

### 9.4 性能/SLO 候选门禁

外部 Provider 延迟不能被承诺为绝对值，因此性能指标分为平台可控与外部观测：

- 平台可控：目标负载下 Chat queue wait P95 ≤ 2 秒，Proposal/Plan/Group queue wait P95 ≤ 10 秒；
- 平台可控：无 429 时内部状态提交和恢复调度 P95 ≤ 2 秒；
- Provider 观测：first payload、完整 Turn 和 Token 吞吐分别报告，不与 queue wait 混合；
- 30 Proposal 压测的 P95 目标在 Provider 基线后冻结，候选初值为 180 秒，超过即阻断而不是延长等待到 50 分钟；
- structured repair rate 候选目标 < 5%，若达到 30/30 但修复率过高，仍不得宣称稳定。

正式阈值必须在固定模型、固定区域、固定并发和固定 SHA 上基线化后写入 CI 配置；文档不以单次最快结果替代 SLO。

## 10. 迁移实施顺序

### Phase 0：止血与证据冻结

- 冻结对 Runtime Tool 授权、Prompt 和结算的零散补丁；
- 保留当前 UAT 数据和失败 Run；
- 将当前自然 Chat/Group、Proposal 30 次和 Trace 不一致写成 characterization tests；
- 不直接 revert `d1434ef`，避免恢复未经结构化裁决的隐式能力。

退出条件：固定 SHA、失败样本、数据库快照和可重复测试齐全。

### Phase 1：Capability Envelope

- 实现机器态能力裁决和快照；
- 删除文本正则对 Tool 暴露的影响；
- 保留 Tool Dispatcher 二次校验、Graph、Principal 和 Confirmation；
- 增加 assembled/effective/exposed 对账 API/Trace。

退出条件：自然语言变化不改变权限；所有越权结构化调用仍被拒绝。

### Phase 2：Model Turn 内核

- 引入 ModelTurn/ProviderAttempt、Response Contract、错误分类和预算；
- 抽离 Provider Adapter 与 StreamAccumulator；
- 完成 Checkpoint Turn cursor；
- 修复 Trace 终态一致性。

退出条件：Provider 合同测试和首/后 payload 故障注入全部通过。

### Phase 3：Plan/Group 结构化生成

- 接入 Model Capability Profile 和 JSON Schema；
- Plan Proposal、Group Formation 切换为结构化契约；
- 建立有精确错误的单次修复；
- 将 Proposal lane 与交互 lane 隔离。

退出条件：真实模型 Plan 30/30、Group Formation 10/10，修复率和延迟达门槛。

### Phase 4：Chat Harness

- 接入自然 Tool 选择、Effect Ledger、协议伪调用检测；
- 完成 Clarification、Confirmation、Artifact、AgentInvocation 恢复；
- 使用最新正确 AgentVersion 重跑自然 Chat。

退出条件：自然 Chat 10/10 且所有效果有权威记录。

### Phase 5：Plan Frozen Execution Harness

- 引入 Owner coordination `tool_required`；
- 校验 DAG 调度、并行 AgentInvocation、节点等待和 all-settled；
- 结算与业务 outcome 分离。

退出条件：综合 Plan、部分失败、恢复和版本冻结全部通过。

### Phase 6：Group Harness

- 完成节点能力、定向澄清、图内调用、历史查询和 delegation chain；
- 分离自然拒绝与恶意结构化图外调用测试；
- 验证新 GroupRun 版本重新固定和终态不可复活。

退出条件：自然 Group 与安全边界矩阵全部通过。

### Phase 7：观测归集与发布门禁

- 接入持久 Trace 后端和 Dashboard/Alert；
- 修复所有 Outbox Topic/Action 消费覆盖；
- 在固定 SHA 上执行 Unit、Provider Contract、Mock Full-stack、真实模型、故障注入和负载门禁；
- 生成可访问的 CI Run、JUnit、coverage、性能与脱敏证据。

退出条件：三个模式同时通过，不能单独豁免任一模式。

## 11. 灰度、兼容与回滚

- 使用 `harness_version` 按新 Run 固定，运行中不得切换；
- Chat 新 Run 可按 Agent 白名单灰度，冻结 Plan/Group 继续使用创建时的 Harness 版本；
- Capability V2 可先 shadow 计算并与旧集合对比，但 shadow 不发起第二次业务 Tool；
- 数据库迁移先加表/字段、双写、校验，再切读，最后移除旧文本授权；
- 回滚只影响新 Run；已产生 ToolInvocation、AgentInvocation、Clarification 和 Artifact 继续按原状态机结算；
- 不使用“切回旧模型”或“删除失败数据”作为回滚手段。

## 12. 工程治理：防止补丁再次造成系统性塌陷

### 12.1 变更追踪矩阵

任何修改以下区域的变更都必须附带“产品不变量 → 技术设计 → 测试”矩阵：

```text
Tool exposure / authorization
Prompt section / response contract
Model Adapter / streaming
Runner/WorkItem/Run transition
Plan/Group graph or version freezing
Checkpoint/retry/fencing
Settlement/outcome
ExecutionEvent/Trace semantics
```

### 12.2 禁止测试与实现共谋

- 产品 E2E 输入由产品场景维护，不能为通过实现而添加内部 key；
- 协议测试可以使用精确 Tool 名，但必须标记为 Adapter/安全测试；
- 修复缺陷时先加入失败的自然场景，再加入最小单元测试；
- 测试通过不能覆盖产品 UAT 失败；二者冲突时发布阻断并回到设计审查。

### 12.3 共享 Runtime 的跨模式审查

共享 Harness 的每次改动必须至少回答：

1. Chat 根/子 Agent 是否变化；
2. Plan Proposal/执行/结算是否变化；
3. Group Formation/节点工作/结算是否变化；
4. 等待恢复和 Worker lost 是否变化；
5. 权限是否被扩大或错误收窄；
6. 快照、事件和 Trace 是否仍可解释。

不能用“本补丁只修 Group”绕过 Chat/Plan 回归，因为三种模式共用 Harness。

### 12.4 架构守护

- `RuntimeWorker` 只编排，不新增模式业务大分支；
- CapabilityResolver 禁止读取用户消息正文来决定权限；
- ProviderAdapter 禁止直接写领域表；
- SettlementService 禁止从模型文本推断已发生副作用；
- Trace Writer 禁止决定业务状态；
- Outbox 的每个 `(topic, action)` 必须在启动时注册唯一消费者，缺失消费者使部署健康检查失败。

## 13. 第一批修复实施检查点（2026-08-03）

本节记录方案落地状态，不改变前述目标架构或最终完成定义。详细结果见[Harness 修复实现与候选验收报告](../acceptance/phase-one-harness-remediation-2026-08-03.md)。

### 13.1 已落地

| 方案项 | 当前实现 |
| --- | --- |
| Capability Envelope | 删除文本正则授权；以 AgentVersion、主体、模式、阶段和冻结拓扑计算 effective tools；Dispatcher 校验实际暴露集合 |
| Response Contract | Plan/Group 使用封闭 Schema，并按 Registry 能力选择 `json_schema/json_object`；OpenAI-compatible Adapter 透传 `response_format` |
| Completion Contract | 通过 Harness `finalize_response` 控制 Tool 接收封闭效果声明；不把声明本身当作效果 |
| Effect Ledger | 从持久 Tool/Agent/Clarification/File/Artifact/History 记录计算 satisfied/pending/unavailable |
| 有界修复 | 结构失败、完成合同缺失和已完成一次性效果重复均最多修复一次；重复副作用不 dispatch |
| Provider 兼容 | 只在零 payload 时重试瞬时传输失败；记录 qwen thinking 路径不支持 `tool_choice=required`，不伪造协议支持 |
| 恢复快照 | Tool Approval/Retry 保留 Group 拓扑、Input 与 ExecutionPrincipal 快照 |
| 可观测性 | Runner API 暴露 assembled/effective/harness tools、response/completion contract、effect state 和 settlement mode |
| UAT 归集 | Collector 增加外部卷 file exporter、最小权限目录初始化和真实写入烟测 |
| 产品 E2E | 电商退款审计垂类 Agent 在 Chat/Plan/Group 使用自然中文完成真实模型综合场景 |

单元 133/133、Mock 全栈 4/4、真实模型 3/3 已通过；报告仍标记 `uncommitted`，属于本机候选证据。

### 13.2 与完整 Harness V2 的差距

为降低本轮 Schema 变更风险，ModelTurn、ProviderAttempt 和独立 RunnerCapabilitySnapshot 尚未关系化；当前检查点将必要诊断固化在现有 Runner input/prompt manifest 与 Trace API 中。这能关闭自然能力不可达、结构契约缺失、文字伪执行和重复一次性效果等 P1，但不能替代后续关系化 Attempt、统一预算、执行 lane、Dashboard、30/30 统计门禁和正式 CI 归档。

存量 UAT AgentVersion 仍有 6—7 项过宽 Tool 装配。Runtime 的模式/阶段交集可缩小实际暴露面，但装配层最小权限仍需业务 Owner 通过新不可变版本关闭，不能直接改写历史版本。

### 13.3 当前判定

- 原历史 UAT 报告继续保持“不通过”，失败 Run 和证据原样保留；
- 第一批 Harness 修复候选的确定性与单次真实模型综合 Gate 通过；
- 在固定 clean SHA、正式 CI、Plan Proposal 30/30、Group Formation 10/10 和新最小权限 UAT AgentVersion 复验完成前，正式发布仍阻断；
- 不使用本轮 1 次真实综合场景替代模型稳定性的统计声明。

## 14. 完成定义

Harness V2 只有同时满足以下条件才算完成：

1. 三种模式使用同一 Model Turn、Capability、Checkpoint、Effect 和 Settlement 内核；
2. 用户自然语言不包含任何 Tool key 仍能稳定触发已装配能力；
3. 未装配、越权、图外或已停用能力始终不能执行；
4. Plan/Group 结构化生成使用能力协商和 Schema，而不是仅靠提示词；
5. 模型文本不能伪造成已执行 Tool、Agent 或 Artifact；
6. transport、protocol、validation、business failure 的重试和结算语义彼此独立；
7. 每个 Runner 可以解释实际暴露能力、模型契约、尝试、效果和终态原因；
8. Chat、Plan、Group 的自然语言 UAT、恶意边界测试、故障恢复和负载门禁在同一固定 SHA 上全部通过；
9. 正式 CI 与部署证据可访问，且没有以本地成功或 Mock 结果替代真实模型门禁。

在这些条件完成前，历史 UAT 结论应保持“不通过”，不得以 Agent 配置修正版、单个技术控制用例、本机未提交候选或 29/30 Proposal 覆盖核心产品缺口。
