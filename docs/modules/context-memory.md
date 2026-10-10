# 上下文与记忆

本文是上下文策略、历史窗口、记忆策略和记忆作用域的唯一权威来源。

## 四类策略 Registry

平台代码实现并注册四种彼此独立的策略类型：

```text
Context Assembly Policy
History Window Policy
Memory Write Policy
Memory Recall Policy
```

每个逻辑 Policy Definition 至少包含：

```text
key
type
name
description
current_version_id
status: active | inactive
```

Policy Definition 通过 `current_version_id` 指向不可变 PolicyVersion；PolicyVersion 保存 `version_no + implementation_ref + parameters + content_hash + status`。内部 Token 预算、TTL、排序阈值、存储后端等实现参数属于版本内容，不是 Agent 自定义配置。Draft 选择四个逻辑 Policy，发布时分别固定精确 PolicyVersion；显式 `none` 也是版本化策略，不能用 NULL 表示。

## 各策略职责

### Context Assembly Policy

决定如何组织当前执行所需的业务上下文、Session 状态和 Runtime 元数据，并提供本次输入的软目标和网页结果分配比例；历史消息和长期记忆的选择仍由各自策略负责。模型输入硬限制、权限和必要恢复状态由统一 Runtime 强制执行。

### History Window Policy

决定从当前 UserSession 或 GroupSession 选取、裁剪或总结哪些历史消息。显式 Plan 仍使用原 UserSession，不产生第二套历史源。

### Memory Write Policy

决定 Runner 完成时是否提取记忆、写入何种作用域以及保存哪些来源引用。

### Memory Recall Policy

决定当前输入下召回哪些记忆、如何排序和如何形成 Prompt 段落。

Agent 必须为四个维度各选择一个已启用策略 key。平台可以提供显式的 `none` 策略，但不能以空值形成不明确行为。

## 第一阶段内置策略

### Context Assembly

当前注册：

```text
context.standard
context.concise
context.evidence
```

三者是按任务场景选择的并列方案，不是低、中、高档，也不会按上下文用量自动切换。它们按 [Runtime 执行](runtime-execution.md) 定义的固定 Prompt 区段组织业务上下文和 Runtime 元数据，使用同一装配器，不提供任意脚本、权限覆盖或自定义 Prompt 区段顺序。

| 策略 | 元数据 Token 预算 | 最多产物元数据条数 | 总输入软目标 / 硬上限 | 网页结果预算 / 硬上限 |
| --- | ---: | ---: | ---: | ---: |
| `context.standard` | 8,000 | 20 | 80% | 20% |
| `context.concise` | 4,000 | 5 | 50% | 10% |
| `context.evidence` | 8,000 | 40 | 80% | 35% |

表内是平台注册默认值；管理员可以通过 Policy PATCH 在受控范围内修改。支持的参数为 `token_budget`（1–32,000）、`max_artifacts`（0–100）、`input_target_ratio`（0.1–0.9）、`web_result_budget_ratio`（0.05–0.5）；未知字段、错误类型和越界值拒绝发布。网页摘录分配受总计 12,000 Token 和单结果预算约束；引用元数据先计入预算。必要 URL、来源 ID 等自身超预算时保留真实引用并标记 `shared_budget_exceeded / unavoidable_metadata_tokens`，不宣称达标，随后仍执行完整请求的硬窗口检查。

三种策略另外具有以下受控保留参数：

| 策略 | 软水位保留近期消息数 | 保留最近工具批次数 | 已消费旧工具结果目标 Token |
| --- | ---: | ---: | ---: |
| `context.concise` | 2 | 1 | 512 |
| `context.standard` | 8 | 3 | 1,024 |
| `context.evidence` | 4 | 2 | 2,048 |

默认量分别服务于以下场景：

- 简洁问答：保留最近一次问答和一批工具调用，较早大结果保留简短状态、定位和摘录，避免无关历史占用。
- 持续通用型长任务：保留更宽的近期对话和三批工具步骤，支持用户修正及准备、执行、验证之间的衔接；较早大结果适度缩减，给持续执行留出空间。
- 证据优先：保留两批工具便于近期材料对照，为每份较早结果分配更大的摘录预算；普通聊天历史少于长任务，将空间留给来源、引用和证据。

这些值是有场景依据的初始默认，不代表经过全部业务样本验证的最优值。软水位和网页份额保持各自用途：长任务和证据都使用 80% 软目标，网页证据份额则由 20% 与 35% 区分，不要求每个参数都形成不同档位。

对应参数为 `recent_history_messages`（0–40）、`recent_tool_rounds`（1–8）、`consumed_tool_result_tokens`（128–8,192）。近期消息数是软裁剪的保护下限，不是 History 召回上限；硬上限超限时仍可能对这些历史做语义摘要。工具批次按 assistant 工具调用消息计数，一批可能含多个并行结果；最新未消费结果、失败和状态工具另受保护。工具结果目标包含完整结果信封，必要来源和状态可能使目标无法满足，不为凑预算裁坏来源信息。

管理 API 返回 `parameter_schema` 与 `current_version_id`。修改策略参数发布新的不可变 PolicyVersion；相同内容复用已有版本。新发布参数补齐当前默认值；旧冻结版本缺少字段时始终使用此次调整前的兼容值（长任务 6/2/1,024，证据 10/3/2,048），不随新默认值漂移。既有 AgentVersion 固定的策略引用不变，只有新发布的 AgentVersion 使用新的当前策略。

API 启动时为尚未修改、启用且内容精确匹配旧内置默认的策略幂等发布新版本；包括最早只保存 `token_budget=8000` 的标准策略。管理员已编辑的策略、停用策略和非内置实现不自动改写。策略升级不需要数据库迁移，也不改变 History 的召回和滚动摘要参数。

### Agent 装配方案

Agent 配置页提供以下组合，仍只保存已有策略 ID：

| 方案 | Context Policy | History Policy |
| --- | --- | --- |
| 简洁问答 | `context.concise` | `history.recent` |
| 持续通用型长任务 | `context.standard` | `history.summary_recent` |
| 证据优先 | `context.evidence` | `history.recent` |

构建者可以继续在高级配置中分别选择策略，其他组合显示“自定义”。方案只调整 Context / History，不修改 Memory Write / Recall；缺失、停用或没有已发布当前版本的策略不可选。已有 Agent 不自动切换方案。切换后需先保存再测试或发布，避免把未保存方案误当成正在执行的版本；缺失依赖保持原 ID 并明确显示不可用。证据优先仅为已授权、已有的产物信息和网页结果分配更多空间，不触发搜索、不授予新权限。

### 原文与模型视图

Message、ToolInvocation 完整结果和 Artifact 仍是事实来源；策略不因裁剪或摘要而覆盖、删除这些记录。模型视图可以省略旧消息、截取网页证据或使用派生摘要，无法承诺语义无损。WorkingPlan、摘要和召回信息以数据区段进入模型，不能获得 system 指令权限。

每轮请求的 manifest `sections` 按实际消息顺序记录 `message_index / role / checksum / token_estimate`；历史记录附带来源 Message ID。投影改写时保留 `source_checksum` 并标记 `treatment=projection`。裁剪从实际 `sections` 移除对应项，省略项另记；Runner、AgentTurn 和结算 Checkpoint 使用相应实际请求的清单。工具调用/结果的配对和恢复数据不作为普通历史丢弃。数据保留期与删除另属存储治理，不由上下文方案隐式执行。

### History Window

第一阶段注册：

```text
history.recent
history.summary_recent
```

- `history.recent`：在当前 Agent 的直接可见历史中保留最近窗口，超过平台 Token 预算时截断更早内容。
- `history.summary_recent`：保留当前可见历史的摘要和最近窗口；摘要由平台异步更新。

Token 预算、消息数量和摘要触发阈值属于平台注册配置，不在 Agent 页面填写。摘要失败时可以退化为同一可见范围内的最近窗口，但不能切换 Agent 模型或扩大可见范围。

历史摘要是 Session 历史压缩结果，不是长期记忆。

已有 ready 摘要不再阻止新增历史触发更新。新摘要使用冻结父摘要加后续增量；每批通常最多 200 条消息且源 JSON 不超过 48,000 字节，同时检查冻结模型输入窗口。父摘要文本 hash、边界和完整原文前缀 hash 都必须匹配；生成后再次校验原文，变化则拒绝发布。原文校验按授权范围分页扫描，内存有界，但完整前缀校验仍有随历史增长的读取成本。

单条超大消息采用带消息 ID、原文 hash 和字符区间的分段摘要，单命令最多 192,000 字节源文本、8 次模型调用，只有全部片段完成才推进该消息的覆盖边界。无法在限制内处理时明确退化，不跳过未读片段、不伪称完整覆盖。重试可能重新调用该命令已经完成的部分片段。

更新期间继续使用有效旧摘要和近期窗口；新摘要由维护任务异步生成，在后续装配中生效。manifest 记录 `summary_coverage / summary_refresh / uncovered_message_count / uncovered_range / uncovered_source_hash / degraded_reason`；来源 ID 列表最多 100 条，超过时标记 `uncovered_ids_truncated`。有缺口时另向模型提供覆盖提示，不能推断缺失历史中的约束或证据。同一作用域最多一个 pending，终态失败的相同源边界不重置重试预算。当前输入排除不改变共享摘要的源校验，也不会形成有空洞的摘要源前缀。原文的授权查询能力不等于每个 Agent 都已具备历史回查工具。

### 水位处理与恢复

初次装配和循环内每次调用共用 `context_budget.py`：

1. 从原始结果生成网页视图并对完整请求计数，包含工具 schema 和冻结输出预留。
2. 超过软水位后，缩减较早、已被模型消费的成功工具结果；保留近期工具批次、失败/不确定结果、计划和协作状态工具。缩减内容为明确标记的原文首尾摘录，保留来源、产物和状态，不伪造语义摘要。
3. 请求在硬上限内但超过软水位时，按完整调用/结果组移除旧 History，保留策略规定的近期窗口。完整请求仍超过硬上限时，先进入同步语义压缩，不预先用硬裁剪丢掉压缩来源。
4. 同步压缩把可压缩历史和已消费、完整的工具续接范围交给独立无工具模型调用，生成任务状态摘要。当前输入、系统边界、WorkingPlan、状态工具和未消费结果保留原样。若这些受保护内容独占预算，直接失败；不以强制压缩为由扩大硬上限。
5. 压缩源最多 192 KB、最多 8 次摘要调用、总超时 120 秒；单次摘要输出最多 1024 tokens，按剩余空间进一步缩小。源按可追溯字符片段分批，每次摘要请求完整计数并保留输出空间。拒绝空输出、工具调用、截断、来源变更、无缩减收益和压缩后仍超限；全部完成且重新计数通过后才安装摘要视图。
6. Provider 实际拒绝长度时，仅在没有收到任何响应内容的条件下，最多尝试一次有界语义压缩后重新请求。已收到部分文本或工具调用时不透明重试，不重新执行已有工具副作用。

证据优先不会因为模型进入下一轮，就把所有网页结果降为单段短摘录；来源 URL、source ID、检索时间和原有 checksum 保留，摘录仍受共享预算限制。每轮从 canonical 原始结果重新投影，`full_result_hash` 不会变成上次投影的 hash。Checkpoint 保存 canonical continuation，实际请求 manifest 保存投影后的校验和，二者职责不同。

硬压摘要以 assistant 数据段呈现，不提升为系统指令；来源身份、原文哈希、摘要哈希、前后计数和 usage 记录在 `hard_compactions`。摘要独占一个 manifest 标记 `purpose=context_compaction` 的 AgentTurn，复用 ProviderAttempt 和费用账本，计入任务调用/token预算，不向用户串流或执行工具；后续业务请求中的摘要数据段仅标记 `source=context_compaction`，不代表再次调用摘要模型。网络调用前释放数据库事务，结果安装前重新核验 Runner 所有权。

摘要缓存随 Runner input 和安全 Checkpoint 传递，绑定 Run、WorkItem、AgentVersion、ModelVersion；恢复合并检查点和较新已付费缓存，再核对源身份与完整内容，只有唯一匹配的有效范围才重放，重叠范围优先使用较新缓存。原始 Message、ToolInvocation 和 canonical pending_results 不被摘要覆盖；跨任务不复用此执行缓存。常规异步 HistorySummary 仍独立运行。

### 参考与验证边界

- Claude Code 官方说明先清理旧工具输出、必要时再摘要；Anthropic 的公开 context editing 提供近期工具对保留和排除工具配置。本项目借鉴这些保护边界，没有宣称复制所谓固定“五层内部实现”。[Claude Code 上下文说明](https://code.claude.com/docs/en/how-claude-code-works#when-context-fills-up)、[Anthropic context editing](https://platform.claude.com/docs/en/build-with-claude/context-editing#configuration-options-for-tool-result-clearing)。
- Codex `0.153.4` 的公开代码展示了阈值触发 compaction、完整上下文预算检查、压缩请求预裁及失败后的有界恢复。本项目沿用可观察、可恢复的原则，未把 OpenAI 私有远程压缩作为跨 Provider 前提。[Codex 调度](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/core/src/session/turn.rs#L1053-L1080)、[压缩失败恢复](https://github.com/openai/codex/blob/3d2ee51ca2d5db578f328aa75e20aa22c0197c9a/codex-rs/core/src/compact.rs#L314-L346)。
- 硬压采用“独立摘要请求自身仍须装进窗口、成功后安装、失败有界”的原则。[Anthropic 按需压缩](https://platform.claude.com/docs/en/build-with-claude/compaction#compact-on-demand-with-the-compaction-parameter)、[OpenAI compaction](https://developers.openai.com/api/docs/guides/compaction)。本项目用有界分块覆盖全部选中来源，未照搬 Codex 摘要请求超限时删除最早输入的降级方式，也未接入供应商专用加密 compaction item。

自动回归检查策略选择、输入保留、成组裁剪、证据出处、多轮预算、增量覆盖、权限隔离和恢复配对；这些测试验证运行时合同，不证明所有真实模型在任意长任务中的语义保真或完成率。真实 Provider 的长任务质量和成本仍需在受控评测集上衡量。

### Memory Write

第一阶段注册：

```text
memory_write.none
memory_write.user_agent_facts
memory_write.user_facts
```

- `memory_write.none`：不写长期记忆。
- `memory_write.user_agent_facts`：从用户明确表达或确认的信息中提取稳定事实，写入 `user_agent` 作用域。
- `memory_write.user_facts`：只提取适合跨 Agent 使用的稳定用户事实，写入 `user` 作用域。

模型推测、临时计算、内部分析过程和未经用户确认的 Tool 结果不能直接写为长期记忆。记忆写入异步执行；失败记录 ExecutionEvent，但不把已经完成的原 Run 改为失败。

### Memory Recall

第一阶段注册：

```text
memory_recall.none
memory_recall.user_agent
memory_recall.user_combined
```

- `memory_recall.none`：不召回。
- `memory_recall.user_agent`：只召回同 `tenant_id + account_id + agent_id` 的记忆。
- `memory_recall.user_combined`：召回当前 Agent 专属的 `user_agent` 记忆和同用户的 `user` 记忆，优先专属记忆。

召回结果必须记录使用过的 `memory_id` 到 Runner 的可解释快照或 Trace。策略可以排序和裁剪召回结果，但不能越过作用域查询。

## 记忆作用域

### `user_agent`

同一用户与同一 Agent 跨 Session 共享。适合该用户与某个领域 Agent 的偏好、口径和持续事项。

### `user`

同一用户跨 Agent 共享。适合多身份、多 Agent 协作时需要复用的用户级事实。召回仍由当前 Agent 的 Memory Recall Policy 决定。

### `agent`

同一 Agent 跨用户共享。当前只保留数据模型与策略接口，不在现阶段启用写入和召回，用于后期受审核的 Agent 进化。

所有记忆必须记录作用域键、来源 Session/Runner、创建策略和可审计元数据，避免跨作用域误召回。

第一阶段 Memory 至少记录：

```text
memory_id
account_id
agent_id?
scope
content
source_session_id
source_message_ids[]
created_by_runner_id
policy_key
status
created_at
updated_at
```

相同或冲突事实需要受控更新、合并或停用，不能无界重复追加。用户可以查看、修改和删除自己的记忆；删除或停用后不得继续召回。

复用同一 Runner 的记忆快照时，仍重新检查 active 状态、租户、用户、Agent 作用域及内容 hash。快照固定已选记忆的身份和顺序，不保留对已撤回内容的访问权；失效项被排除，不自动补选其他记忆。

## Group 中的上下文

Group 只有一个共享 GroupSession，不为每个成员复制一份用户会话。每个成员以 `group_id + agent_id` 为私有执行命名空间，先确定当前 Agent 的直接可见范围，再由自己的策略在该范围内构建上下文。

直接可见范围先经过 `private | direct | group | user` 可见性投影，包括：

- 当前用户目标、Group Membership、RunSnapshot、WorkingPlan 和整体状态；
- 当前 Agent 自己发送和接收的消息；
- 用户定向发给当前 Agent 的消息；
- 当前 Agent 发起或收到的 Clarification；
- 当前 Agent 认领或收到的 WorkItem / SharedTask；
- 当前 Agent 参与的 AgentInvocation 请求与结果；
- 明确发送给当前 Agent 的 Artifact；
- 当前 Agent 自己的 ToolInvocation 和执行结果。

Context、History 和 Memory Policy 只能在上述可见范围内裁剪、召回和组装，不能借助策略读取其他 Agent 的对话或工作。

其他 Agent 之间的 human 可见定向 `@` 委托/交付、标记为 `group` 的精炼留痕、SharedTask、调用、验证结果、分歧、结果和 Artifact 属于可主动查询的 Group 共享记录。未参与成员不会收到通知，这些记录也不会自动进入其 Prompt；成员只有显式绑定且当前可用的 `search_group_history` 才能主动查询。查询结果进入当前 Runner，并保留来源、sequence、visibility 与 checksum。`direct` 只扩大群聊记录的主动检索范围，不会暴露其他 Agent 的私有 Runtime 事件、Prompt、Tool 数据或 Memory。

自动装配的产物目录仅包含当前 Agent 自己发布的产物，或通过其直接可见消息明确交付的附件。同 Session、同用户、已发布或存在 Group 共享索引，均不单独构成自动注入其他 Agent 上下文的许可；委派 Agent 不继承父会话的产物目录。此目录只有元数据，不包含正文或私有路径。

GroupTranscriptDocument 是共享 Message 与 ExecutionEvent 的派生只读文档或索引，不是上下文策略的隐式数据源。`search_group_history` 是 Group 模式的只读 Runtime 协调工具，必须通过 AgentVersion 显式绑定并在平台启用；Runtime 仍须按当前 Membership 和 GroupSession 逐次授权。

`history.summary_recent` 的 Group 摘要也必须按 `group_id + agent_id` 分别维护，不能生成一份全群摘要后自动注入所有 Agent。通过 `search_group_history` 得到的结果只进入当前 Runner 的可见上下文，不自动写入长期记忆。

共享 GroupSession 不等于共享私有配置。其他成员不能读取本 Agent 的 Instructions、私有记忆、Runner Prompt、模型内部推理或敏感凭证。平台也不保存或传播原始 Chain-of-Thought；需要协作的解释应是 Agent 主动生成的消息、决定摘要、事实引用和风险。

## 后期 Agent 进化

进化不是当前执行能力。后期候选流程为：

1. 从 Session/Runner 快照提取可审计的事实记忆；
2. 跨用户聚合过程性经验；
3. 生成 Skill Candidate，而不是直接修改 Agent；
4. 管理员审核内容、来源和风险；
5. 通过后发布为新 Skill 或 Skill 新版本。

系统不得自动修改 Agent Instructions、装配项或已发布 Skill。
