# Runtime 演进取舍：MyClaw 与 Codex Harness 对照

> 评估日期：2026-09-16
> 文档性质：架构取舍与实施优先级，不替代现有产品、数据库、状态机和 OpenAPI 契约。
> 适用范围：模型内环、上下文压缩、Runtime 宿主协议、Runtime Worker 拆分四项优化。

## 1. 结论

MyClaw 不应该演进成 Codex 的云端复刻版。两者有重叠，但核心责任不同：

- Codex Harness 首先解决单个代码 Agent 如何高保真地理解工作区、执行命令、修改文件并把过程呈现给宿主；
- MyClaw 首先解决多个长期存在的企业 Agent 如何在租户、身份、权限、预算和数据边界内，被按需运行、持久等待、异步协作和可靠恢复。

因此，本轮优化应采用以下总策略：

1. 保留 MyClaw 的 `Run / WorkItem / Runner / Invocation / ExecutionEvent / Outbox` 作为唯一权威状态；
2. 保留 Provider-neutral 的最小模型协议，不把所有业务动作改造成 Codex 风格的模型 Item；
3. 增加统一的 Runtime 事件词汇和生命周期投影，但暂不新增第二套 Item 权威账本；
4. 优先修正上下文和结算的正确性，再增强表现力与外部接入；
5. 暂不建设通用 App Server；有真实 IDE、CLI、外部 Harness 或宿主 Tool 接入需求时再建设 Runtime Gateway；
6. Runtime Worker 必须模块化，但保持模块化单体，不提前拆成微服务。

最终分类如下：

| 优化方向 | 总体判断 | 当前动作 |
| --- | --- | --- |
| 模型内环全面复制 Codex Item 模型 | 没有必要 | 只吸收稳定 ID、生命周期和少量安全摘要能力 |
| 统一 Runtime 事件语义 | 必须 | 基于现有领域实体和 ExecutionEvent 做投影 |
| 上下文关键状态保护、摘要可观测、大结果外置 | 必须 | 作为最高优先级推进 |
| 所有被裁剪历史都要求无损语义覆盖 | 没有必要 | 保护机器事实，允许显式、可观测的旧对话退化 |
| 公共 JSON-RPC/WebSocket App Server | 可选 | 延后到出现真实外部宿主需求 |
| durable cancel 与安全边界上的 steer | cancel 必须，steer 重要但可分期 | 先做持久命令语义，不依赖 WebSocket |
| Runtime Worker 内部职责拆分 | 必须 | 行为保持式重构，不拆部署单元 |
| Runtime/Control/Group 立即拆成多个微服务 | 没有必要 | 先拆代码边界，按指标决定是否拆进程 |

## 2. 判断基准

### 2.1 MyClaw 的产品不变量

本评估以 [产品定位](../PRODUCT.md#产品定位)、[设计哲学](DESIGN_PHILOSOPHY.md)和[Runtime 执行契约](modules/runtime-execution.md)为约束，特别是：

- Agent 是持久逻辑主体，不是进程；
- Chat、Plan、Group 共用一个 Agent Loop；
- Group 是工作网中涌现的逻辑关系，不是固定角色图；
- 模型决定专业路径，平台垄断身份、权限、副作用和持久状态；
- 等待后创建新 Runner，旧进程不需要恢复；
- Group 的定向请求和交付 human 可见，但只唤醒接收者；
- Redis、流式 delta 和 UI 投影不是业务事实源；
- 最少抽象优先，新实体必须证明现有原语无法承担。

### 2.2 分类定义

**必须**表示不做会破坏当前产品承诺，或显著增加错执行、越权、不可恢复、上下文错误和维护事故的概率。

**可选**表示有真实价值，但价值依赖外部宿主、调用规模、Provider 能力或用户行为；缺少触发条件时不应提前支付复杂度。

**没有必要**不表示永远禁止，而是当前方案重复已有权威状态、偏向代码 Agent 特性，或者收益不足以覆盖一致性和迁移成本。

判断优先级遵循：

```text
正确性与权限
> 可恢复性与幂等
> 可诊断性
> 产品体验
> 扩展便利
> 协议形式统一
```

## 3. Codex 已公开能力与证据边界

以下是截至评估日期，OpenAI 官方文档已经公开的能力，不是推测：

- Codex App Server 使用双向 JSON-RPC，支持 `initialize`、thread start/resume/fork、turn start/steer/interrupt；
- 公开 `item/started`、各种 delta、`item/completed` 生命周期，completed Item 是最终权威表现；
- Item 已包含 message、plan、reasoning、command execution、file change、MCP、dynamic tool、collaboration、review 和 context compaction 等类型；
- 支持服务端发起审批、用户输入请求和实验性的动态 Tool 调用；
- 支持主动触发 thread compaction，并以 `contextCompaction` Item 表达过程；
- WebSocket transport、dynamic tools 和部分权限接口仍标记为实验性；官方明确说明 App Server 的 WebSocket 传输当前不支持生产工作负载；
- 实验性的 paginated history 尚不能完整创建、恢复和分页 Turn，官方文档明确采用 fail-closed 行为。

官方来源：

- [Codex App Server](https://learn.chatgpt.com/docs/app-server)
- [OpenAI Agents API：Subagent Items](https://developers.openai.com/api/reference/typescript/resources/beta/subresources/agents/subresources/sessions/subresources/subagents/subresources/items/methods/list)
- [OpenAI Responses：Conversation Compaction](https://developers.openai.com/api/reference/java/resources/beta/subresources/responses/methods/compact)

公开资料不能证明 Codex 内部 Worker 如何拆分、数据库如何结算、是否使用与 MyClaw 类似的 lease/fencing，也不能证明其未来路线。因此本文不把这些内容写成 Codex 现状。

## 4. 优化一：模型内环表达能力

### 4.1 当前事实

MyClaw 当前 Provider 协议刻意保持最小：`assistant_message`、`tool_call`、stop reason、usage、stream event 和受控的 provider state reference。见 [`protocol.py`](../backend/app/runtime/protocol.py)。

业务语义已经由领域实体表达：

- Tool 行动由 `ToolInvocation` 表达；
- Agent 委托由 `AgentInvocation` 表达；
- 人类交互由 `Clarification / Approval` 表达；
- 计划由 `WorkingPlan / PlanUpdate` 表达；
- 交付由 `Message / Artifact` 表达；
- 可靠时间线由 `ExecutionEvent` 表达。

这不是简单的“协议落后”。它保持了跨 Provider 的可移植性，也避免让模型输出格式成为业务权威。

真正的缺口是：这些领域事实缺少统一、稳定、可供 UI 和外部客户端消费的 Runtime 事件语义。当前文本流、工具进度和领域事件并未完全形成一套统一生命周期。

### 4.2 必须做

#### A. 建立统一 Runtime Event Vocabulary

建议在现有 `ExecutionEvent` 之上定义版本化投影，而不是立即创建 `AgentTurnItem` 表。核心事件至少包括：

```text
message.started / delta / committed
tool.requested / waiting_approval / running / completed / failed
agent.requested / accepted / delivered / failed
plan.updated
clarification.requested / resolved
approval.requested / resolved
artifact.published
context.compaction_requested / ready / used / failed
run.waiting / resumed / completed / failed / cancelled
```

每个事件需要稳定携带：

```text
schema_version
event_id / sequence
session_id / run_id / work_item_id / runner_id / agent_turn_id?
entity_type / entity_id
correlation_id / causation_id
visibility / recipient
status
payload 或 artifact_ref
```

这套投影解决 UI、诊断和未来协议接入问题，但不参与业务结算。最终状态仍读取 Run、Invocation、Message 等领域实体。

#### B. 稳定模型调用内的关联关系

必须确保：

- Tool Call ID、Tool Result、AgentTurn 和 Invocation 可稳定关联；
- Provider 流式事件失败后，已提交的 Item/Invocation 不被重复创建；
- Provider-specific continuation 只作为不透明引用，不泄漏成共享业务状态；
- 不支持的内容模态明确失败，不静默转成文本；
- 完整 Provider 响应与流式临时输出有明确的 commit/reset 边界。

#### C. 为安全的 reasoning summary 留扩展点

可以允许 Adapter 返回“供应商明确允许展示的推理摘要”，但它必须：

- 与原始 Chain-of-Thought 区分；
- 默认不进入 Group 共享上下文；
- 不成为完成、验证或权限判断依据；
- 可按 Provider 能力降级为空。

这里必须的是扩展点和安全边界，不是要求所有模型都提供 reasoning summary。

### 4.3 可选

- 在外部 Runtime Gateway 出现后，为 Runtime Event 增加 `started/completed` Item 兼容投影；
- Provider 原生 Item 的有限保留，用于诊断和高保真回放；
- plan、command、file change 的专用 UI Item；
- 针对某个 Provider 的原生 compaction/continuation；
- 独立的 `agent_turn_item` 表。

只有当以下需求出现时，`agent_turn_item` 才值得新增：

1. 外部客户端必须无损分页和回放一个 AgentTurn 的全部 Item；
2. 现有 AgentTurn output、Invocation、Message 和 ExecutionEvent 无法稳定重建；
3. 数据保留量、索引和隐私成本经过测算；
4. 明确规定它只是执行账本，不是第二套业务状态机。

### 4.4 没有必要

- 把 `invoke_agent`、`ask_user`、`update_plan` 等 Tool 改造成模型专用协议动作；
- 把 `commandExecution`、`fileChange` 作为所有 Agent 的通用模型输出类型；
- 保存原始 Chain-of-Thought；
- 为了匹配 Codex UI，把每个 token delta 持久化到数据库；
- 让 Runtime Item 决定 Run 是否完成。

Codex 的丰富 Item 很大程度服务于代码编辑器和本地工作区体验。MyClaw 可以在表现层吸收其优点，但业务动作仍应经过现有 Dispatcher 和领域实体。

### 4.5 Codex 对照

| 能力 | Codex 现状 | MyClaw 决策 |
| --- | --- | --- |
| 统一 Item tagged union | 已有 | 不完整复制；先做领域事件投影 |
| started/delta/completed | 已有 | message/tool/run 生命周期必须统一 |
| command/file 专用 Item | 已有 | 仅作为 Tool UI 投影，可选 |
| collab Item | 已有 | AgentInvocation 继续做权威事实 |
| reasoning summary/raw reasoning | 已有接口 | 只接受安全摘要；原始推理不保存、不共享 |
| dynamic client Tool | 实验性 | 等真实宿主场景再做 |

## 5. 优化二：上下文压缩

### 5.1 当前事实

MyClaw 已有异步语义摘要，不是只有“删除最老消息”：

- `HistorySummary` 有 `pending / ready / failed / superseded`；
- 使用 `through_message_id / through_event_sequence / source_hash` 验证覆盖前缀；
- Chat 与 Group Agent 按可见范围分别维护；
- 摘要通过 Outbox 和 maintenance worker 异步生成。

相关实现见 [`history_service.py`](../backend/app/services/history_service.py)、[`history_v1.py`](../backend/app/models/history_v1.py)和 [`maintenance_command_service.py`](../backend/app/services/maintenance_command_service.py)。

同时，ContextAssembler 和 Provider 精确计数阶段仍会按预算删除较早历史。见 [`context_assembler.py`](../backend/app/runtime/context_assembler.py)和 [`runtime_worker.py`](../backend/app/workers/runtime_worker.py)。

这两层并存本身合理：后台摘要优化长期历史，最后裁剪保护单次请求不会超过模型窗口。问题不在“允许裁剪”，而在必须确保关键工作状态不依赖旧自然语言历史存活。

### 5.2 必须做

#### A. 定义不可压缩的关键状态

以下内容必须以结构化权威状态进入 Context，不能只存在于旧聊天：

- 当前用户目标和本轮新增约束；
- 当前权限、预算和环境边界；
- 活跃 WorkingPlan；
- 未消费的 Tool、Agent、Clarification 和 Approval 结果；
- 待处理 Invocation 及其 operation key；
- 当前 GroupRun 成员快照、接收者和可见性；
- 已确认但仍影响后续行动的关键决定。

前六项已有较强基础。第七项不应通过“把所有历史都永久保留”解决，而应在 WorkingPlan、WorkItem checkpoint 或明确的 decision summary 中结构化保存。

#### B. 统一预算执行，策略按 Agent 装配

平台统一硬预算、权限、必要恢复状态和最终审计；内容取舍由 Agent 选择的版本化策略决定。构建者选用“简洁问答 / 持续任务 / 证据优先”等现有 Context + History Policy 组合，Memory Write / Recall 仍独立。无需增加另一个 ContextProfile 权威实体。具体参数和版本语义见[上下文与记忆](modules/context-memory.md)。

`ContextBudgetPlanner` 是后续可抽取的纯预算决策边界，不把数据库访问、Provider 网络计数和领域结算装进同一对象。预算执行需要协调：

1. 计算不可压缩区段；
2. 选择 ready HistorySummary；
3. 选择 recent history；
4. 外置大型 Tool/检索结果；
5. 接收 Provider token count 或明确标记的保守降级计数，决定最终裁剪；
6. 生成与实际请求一致的 manifest。

`select_history` 可以继续负责可见性和策略选择，但最终 included/compacted/externalized/omitted 必须由同一预算结果记录，避免选择层和装配层各自做一次不透明裁剪。

策略改变发给模型的视图，不覆盖 Message、完整 Tool 结果或 Artifact 原文。摘要、片段和引用是可追溯的派生信息；源内容删除和保留期属于独立治理。

#### C. 大结果外置

大型 stdout、网页正文、文件内容、检索结果和 JSON 应保存为 Artifact 或受控 Result Blob。模型上下文只保留：

- 结果摘要；
- checksum；
- 稳定引用；
- 必要片段；
- 继续读取的方法。

未消费结果不能直接丢弃，但也不能因为一个超大结果永久阻塞后续所有 Turn。

#### D. 压缩必须可诊断

Runner prompt manifest 至少记录：

```text
history_policy_version
summary_id / source_hash / through_sequence
included_message_range
omitted_message_count / omitted_range / content_hash
externalized_result_refs
pre_compaction_tokens / post_compaction_tokens
soft_target / hard_limit
degraded_reason
```

Group 摘要必须按实际 GroupSession、Membership 和 GroupRun 作用域校验，不能生成一份全群摘要自动注入每个成员，也不能跨运行复用不匹配的源投影。

#### E. 2026-09-17 上下文首批实施

已实现三种 Context 策略和 Agent 配置组合、受控参数校验与不可变 PolicyVersion 发布；保留旧 Agent 引用。Runtime 按所选版本应用元数据条数、输入软目标和网页结果比例，硬限制不变。

后续补强将摘要改成冻结父摘要加有界增量：通常每批 200 条 / 48,000 字节，完整原文前缀分页校验；超大单消息带区间分段，单命令最多 192,000 字节和 8 次模型调用，全部成功后才推进覆盖。旧摘要、近期窗口和异步缺口分别记录；摘要失败不假覆盖、不改变 Group 可见范围。

初始请求和循环续跑共用 `context_budget.py`。三种方案有不同近期消息数、近期工具批次数和旧结果预算；软水位先缩减已消费成功结果，再按完整工具配对裁历史。硬限制前保留当前目标、WorkingPlan、失败和未消费结果；无法容纳时明确终止。Provider 超窗只允许零载荷、实际缩减后的一次恢复。History 候选二分计数，避免每删一条就发一次远程计数请求，记录 `count_calls`。

每轮从 canonical 结果投影，实际请求 manifest 对齐投影 checksum，恢复 checkpoint 保存完整 continuation。异步等待保留本 Runner 前面的同步结果，补齐待完成批次时不重复 call/result，并继承动态加载工具版本。证据优先保留来源引用，避免仅因下一轮开始就把全部旧网页结果变成短摘要；必要引用预算超额明确标记。

已实现的父摘要引用冻结在 Outbox payload，未新增摘要父链数据库列、通用 Blob 状态机或运行时事件词汇。界面阻止未保存上下文方案被误测试/发布。参数、参考来源和边界见[上下文策略契约](modules/context-memory.md#水位处理与恢复)。

本次补强验证：后端全量单测 **345 passed**；前端 **13 项测试通过**，TypeScript 与 Vite 生产构建通过；Chrome mock API 验证方案切换、保存、回显、依赖不可用及测试/发布阻止。未运行真实 Provider 质量评测或 MySQL 并发验收，未执行部署。增量摘要的原文前缀校验仍有 O(n) 读取成本，新摘要在后续装配生效，不能承诺无限长任务或语义无损。

### 5.3 可选

- 将已冻结在 Outbox payload 的父摘要引用提升为专用数据库关系（仅在查询需求出现时）；
- 手工“立即压缩”按钮或 API；
- 针对长任务的压缩质量 Eval；
- Provider 原生加密 compaction item；
- prompt cache breakpoint 优化；
- 在硬限制前进入短暂 `waiting_resource` 等待压缩。

最后一项只适合不可压缩状态仍接近 hard limit 的极端场景。普通对话不应因为后台摘要暂时未完成而被强制阻塞。

### 5.4 没有必要

- 要求每条被省略的旧消息都得到无损语义覆盖；
- 将全部聊天历史向量化后每轮重新召回；
- 把 HistorySummary 当长期 Memory；
- 因摘要失败而扩大 Group 可见范围或切换到另一个 Agent 的摘要；
- 再创建一套与 `HistorySummary` 重叠的 `ContextRevision` 状态机；
- 让摘要文本获得 system 指令权限。

旧对话是环境信息，不是账本。MyClaw 必须保证机器事实和未完成工作不丢，但不应承诺模型永久看到所有闲聊细节。摘要失败时显式退化到最近窗口，是可接受且更稳健的复杂度取舍。

### 5.5 Codex 对照

Codex 已有 `thread/compact/start`、`contextCompaction` Item 和 Responses conversation compaction。值得借鉴的是“压缩是显式生命周期事件”，不是照搬其压缩载荷。

MyClaw 仍需要自己的摘要和 manifest，因为：

- 支持多 Provider；
- Group 有按成员隔离的可见范围；
- WorkItem 和 Invocation 是跨 Runner 的权威状态；
- 云端审计需要知道使用了哪一段摘要和数据范围。

## 6. 优化三：通用 App Server 协议

### 6.1 当前事实

MyClaw 已经有适合 Web 云平台的交互模型：

- REST/OpenAPI 接收命令和查询；
- MySQL ExecutionEvent 是可靠事件源；
- SSE 使用 sequence 和 `Last-Event-ID` 恢复；
- 前端断线后先从数据库补齐，再接 Redis 临时 delta；
- 客户端断开不影响后台 Run。

实现见 [`sessions.py`](../backend/app/api/sessions.py)、[`live_stream.py`](../backend/app/runtime/live_stream.py)和 [`frontend/src/api/sessions.ts`](../frontend/src/api/sessions.ts)。

这不是 App Server 的残缺版本，而是不同产品边界。浏览器、移动端和企业系统并不天然需要持有一个双向 Runtime 连接。

### 6.2 必须做

#### A. 保持命令与事件分离

继续使用：

```text
REST command/query
+ reliable SSE events
+ provisional Redis deltas
```

所有写命令必须幂等，所有可恢复状态必须进入数据库。SSE 和未来 WebSocket 都只是投影，不拥有执行状态。

#### B. 版本化外部事件 Schema

第三节的 Runtime Event Vocabulary 必须进入公开契约，支持：

- schema version；
- visibility filtering；
- cursor resume；
- unknown event forward compatibility；
- committed 与 provisional 区分；
- 明确的 retention/cursor-expired 语义。

#### C. durable cancel

取消必须是数据库中的持久意图：

- 阻止新的 Tool/Agent 行动；
- 已经发生的副作用不伪装回滚；
- 迟到结果可以记录，但不能重新激活终态 Run；
- Worker 通过 fencing 避免失效执行者继续推进。

### 6.3 重要但可分期：steer

长任务中用户修正目标符合 MyClaw 的产品定位，但不需要先建设 JSON-RPC。

建议先实现 REST command：

```text
POST /runs/{id}/steer
expected_run_version
client_request_id
input
```

语义是：

- 在安全边界前进入当前执行的下一 AgentTurn；
- 已经开始的非幂等副作用不会被撤销；
- 当前 Runner 无法安全接收时，写入 pending input 并由 replacement Runner 消费；
- 全过程写 ExecutionEvent。

只有在需要极低延迟交互时，才把同一命令映射到 WebSocket。

### 6.4 可选：Runtime Gateway

出现以下至少一个真实场景后再启动：

- IDE 或桌面端需要直接宿主 MyClaw Runtime；
- 第三方 Harness 需要接管模型内环；
- 客户端需要提供 session-scoped dynamic tools；
- 外部 Agent 通过长连接接收定向工作；
- 需要 stdio/Unix socket 的本地开发适配器。

届时可以建设 transport-neutral Runtime Gateway：

```text
initialize / capabilities
session.start / resume
run.start / steer / cancel
events.subscribe
approval.respond
clarification.respond
host_tool.result
```

云端可使用生产级 WebSocket，CLI 可使用 stdio bridge，但二者必须映射到同一 Application Service，不能直接操作数据库。

### 6.5 没有必要

- 当前立即复制 Codex App Server 的完整 JSON-RPC 方法集；
- 用 Codex 的 thread/turn 名称替换 MyClaw 的 Session/Run/AgentTurn；
- 为普通 Web Chat 改用双向 WebSocket；
- 把连接存活等同于 Run 存活；
- 让客户端 dynamic tool 绕过 CapabilitySnapshot、Approval 和 ToolInvocation；
- 为本地 stdio/Unix socket 协议提前承担长期兼容成本。

### 6.6 Codex 对照

Codex App Server 已经实现了这套宿主协议的大部分功能，但其主要场景是本地或受控宿主。官方仍将 WebSocket transport 标为实验性和非生产支持，这恰好说明 MyClaw 不应把它直接当成云平台生产协议模板。

MyClaw 当前更需要稳定的 REST/SSE 业务契约，而不是更多传输方式。

## 7. 优化四：Runtime Worker 职责拆分

### 7.1 当前事实

当前 Worker 已经有：

- 有界异步 Runner 槽；
- Provider Client 连接池；
- lease、heartbeat、execution epoch 和 fencing；
- Provider 调用前的短事务提交；
- durable tool/agent wait 与 replacement Runner；
- expired Runner recovery。

因此当前问题不是“没有线程池”或“Worker 只能串行执行”。

问题在于 [`runtime_worker.py`](../backend/app/workers/runtime_worker.py)、[`action_dispatcher.py`](../backend/app/runtime/action_dispatcher.py)和 [`agent_loop_coordinator.py`](../backend/app/runtime/agent_loop_coordinator.py)共同承载了过多策略、事务和结算分支。Chat、Plan、Group、Schedule、AgentInvocation、Group quiescence 和恢复逻辑很容易在修改时互相影响。

### 7.2 必须做

#### A. 先提取纯决策，再移动事务

优先提取：

```text
PromptPolicyComposer
ContextBudgetPlanner
ContinuationDecider
GroupQuiescenceDecider
RunnerOutcome
SettlementDecision
```

这些组件输入冻结事实，输出决定，不直接执行远程 I/O。

#### B. 统一结算入口

目标结算流程：

```text
读取并锁定权威实体
→ 校验 fence / execution_epoch / version
→ 根据 RunnerOutcome 计算 SettlementDecision
→ 更新 Run / WorkItem / Runner / Invocation / Message
→ 写 ExecutionEvent
→ 写 Outbox
→ 单事务提交
```

不同场景可以有不同 CompletionPolicy，但都通过同一个结算框架：

- RootChatCompletionPolicy；
- PlanProposalCompletionPolicy；
- AgentTargetCompletionPolicy；
- GroupMemberCompletionPolicy；
- GroupReporterCompletionPolicy。

#### C. 把 Group 控制逻辑从 Runner 执行中分离

Group quiescence、reporter 唤醒和 summary epoch 属于 Runtime Control handler，不属于模型 Agent Loop。第一阶段可以和 Runtime Worker 运行在同一个进程，但代码、Outbox action 和测试必须独立。

#### D. 拆 Dispatcher 流水线

```text
Normalize
→ Schema Validate
→ Capability Authorize
→ Semantic Risk Classify
→ Approval Resolve
→ Idempotency Resolve
→ Invocation Persist
→ Inline/Durable Route
→ Execute
→ Output Validate
→ Invocation Settle
```

任何 Provider、Tool 或外部 Agent 网络等待都不能跨数据库事务。

### 7.3 可选

- Runtime Control 使用独立 Worker 进程；
- 不同 Tool resource class 使用独立队列；
- 只读 inline Tool 在一次模型并行调用内有界并发；
- 按 Provider、Tenant 或优先级分配 Runner queue；
- Runner preparation 和 settlement 使用独立可扩缩部署。

这些优化需要由以下指标触发：

- claim 延迟；
- active slot 饱和度；
- Provider 等待占比；
- DB pool wait；
- settlement p95；
- Group control backlog；
- Sandbox queue backlog。

### 7.4 没有必要

- 为了缩短文件行数立即拆微服务；
- 为每个 Agent、Group 或 Session 启动独立 Worker；
- 用线程池包装本来就是异步的 Provider I/O；
- 让不同服务各自维护 Run 状态；
- 新增中央 Orchestrator 代替模型决策；
- 以“类数量”或“单文件必须少于多少行”作为架构验收标准。

验收关注的是事务边界、依赖方向、状态机一致性和故障恢复，不是形式上的代码长度。

### 7.5 Codex 对照

OpenAI 官方资料没有公开 Codex 内部 Worker、数据库事务、lease 或 fencing 的实现，因此不能声称 Codex 已经或尚未完成类似拆分。

Codex App Server 的协议边界和 Item 生命周期值得参考，但不能作为 MyClaw 后端执行内核的结构证据。

## 8. Codex 未来可能演进的方向

本节全部是基于公开接口状态的架构推断，不是 OpenAI 路线图或承诺。

| 推断方向 | 置信度 | 依据 | 对 MyClaw 的含义 |
| --- | --- | --- | --- |
| App Server WebSocket、认证、背压和版本兼容走向更稳定 | 高 | 已有实现但仍标记 experimental/unsupported | 不等待它；MyClaw 有真实宿主需求时再建设自己的生产边界 |
| Paginated history 完成创建、读取、恢复和 Turn 分页闭环 | 高 | 官方文档明确当前尚未支持完整流程 | MyClaw 应继续使用自己的 Session/Event cursor，不迁移历史模型 |
| Dynamic tools 和 permission profiles 从实验走向稳定 | 中高 | 已有握手、请求和审批语义 | 未来 Runtime Gateway 可兼容其思想，但仍走 CapabilitySnapshot |
| Codex App Server、Agents API 的 Session/Item/Subagent 语义趋于一致 | 中 | 两边都出现 session、turn、item 和 subagent 结构 | 可以预留 adapter，不把 MyClaw 领域模型改成其镜像 |
| 更丰富的多 Agent 可见性、控制和 UI | 中 | 已有 collab Item 与 Subagent API | MyClaw 的差异化应继续放在企业工作网、Group 可见性和 durable handoff |
| Codex 提供与 MyClaw 等价的多租户、跨 Provider、长期业务 WorkItem 控制面 | 低/无公开依据 | 当前公开重点仍是 Codex 和 OpenAI Agent 生态 | 不应依赖或等待；这是 MyClaw 自己的核心能力 |
| Codex 内部采用某种特定 Worker 拆分或数据库模型 | 无法判断 | 没有公开资料 | 不作为架构决策依据 |

未来兼容策略应是“边界适配”，不是“内部同构”：

```text
Codex / 其他 Harness
        ↓ adapter
MyClaw Application Commands + Runtime Events
        ↓
Run / WorkItem / Invocation / ExecutionEvent
```

如果 Codex 未来稳定提供远程 Harness，MyClaw 可以把它作为一种 `AgentLoopBackend`，但以下能力不能外包：

- Tenant/account authority；
- CapabilitySnapshot；
- Tool/Agent Invocation；
- Approval 与副作用准入；
- Run/WorkItem 生命周期；
- lease/fencing/idempotency；
- Group visibility；
- Usage/cost ledger 和审计。

## 9. 推荐目标架构

```text
Experience
├── Web / API clients
├── REST commands and queries
├── Reliable SSE Runtime Events
└── Optional future Runtime Gateway

Application Services
├── Session / Run commands
├── Collaboration commands
├── Approval / Clarification commands
└── Runtime Event projection

Agent Runtime
├── RunnerPreparationService
├── ContextBudgetPlanner
├── Provider-neutral AgentLoopEngine
├── ActionAdmission / InvocationRouter
├── RunnerOutcome
└── SettlementService

Runtime Control
├── Runner claim / lease / fencing / recovery
├── Group quiescence / reporter wakeup
├── Maintenance / history summary
└── Schedule wakeup

Authority
├── Run / WorkItem / Runner
├── ToolInvocation / AgentInvocation
├── Message / Artifact / WorkingPlan
├── ExecutionEvent / Outbox
└── MySQL
```

关键依赖方向为：

```text
Transport → Application Service → Domain / Outbox
Worker → Application Service / Domain
Provider Adapter → minimal Turn protocol
Runtime Event → domain projection
```

禁止反向依赖：领域状态不能依赖 SSE、WebSocket、Redis delta、Codex Item 或具体 Provider wire format。

## 10. 分阶段实施决策

### 阶段 A：立即推进，先降低正确性风险

1. 先验证每次行动写入的 fence/version，修复上下文清单与真实输入、摘要更新的正确性；
2. 明确不可压缩状态与策略边界，支持按 Agent 选择版本化上下文方案；
3. 完成通用大 Tool/检索结果外置；
4. 在已有行为回归基础上抽取纯预算决策、GroupQuiescenceDecider、ContinuationDecider 和 SettlementDecision；
5. 持续补齐 Chat、Plan、Group、Schedule、Tool wait 和 Agent handoff 状态时间线回归。

完成门禁：

- Context manifest 与实际请求一致；
- 任何等待中的 Invocation 不因历史裁剪丢失；
- Group 可见性不因摘要扩大；
- 所有结算验证 fence/version；
- 状态、事件和 Outbox 同事务提交。

### 阶段 B：增强可诊断性和用户控制

1. 发布 Runtime Event Vocabulary v1；
2. 前端按领域事件展示 Tool、Agent handoff、审批、澄清和压缩状态；
3. 增强 durable cancel；
4. 增加安全边界上的 steer command；
5. 为 safe reasoning summary 增加 Adapter capability，但默认关闭原始推理持久化。

完成门禁：

- 页面断连重连后能恢复 committed 生命周期；
- provisional delta 不会伪装成完成事实；
- steer 不会重复或撤销已经发生的副作用；
- Group 请求和交付只唤醒正确接收者。

### 阶段 C：由真实外部接入需求触发

1. 定义 Runtime Gateway schema；
2. 实现 initialize/capabilities；
3. 映射 run start/steer/cancel 和 event subscribe；
4. 视需求实现 host tool callback、stdio bridge 或 WebSocket；
5. 增加 Codex App Server/其他 Harness adapter。

进入条件：至少有一个需要维护的外部客户端或 Harness 集成，不以“Codex 已经有”为进入理由。

## 11. 本轮明确不做

- 不新增 `AgentTurnItem` 权威表；
- 不新增与 HistorySummary 重叠的 Context 状态机；
- 不复制 Codex App Server 全协议；
- 不把普通 Web Chat 改成依赖 WebSocket；
- 不保存或共享原始 Chain-of-Thought；
- 不把 Group 协作改成中央 Supervisor 或固定拓扑；
- 不按 Agent 拆 Worker；
- 不为了代码结构美观提前拆微服务；
- 不把 Provider 原生 compaction 设为多 Provider Runtime 的共同前提。

## 12. 重新评估触发条件

发生以下任一情况时，应重新评估本文的“可选”和“没有必要”项：

- 第一个正式 IDE/CLI/第三方 Runtime 客户端进入维护；
- 外部 Agent 需要通过长连接领取工作；
- Runtime Event 已无法由现有领域实体稳定重建；
- 历史摘要成本或延迟成为主要瓶颈；
- 超大 Tool 结果导致 hard context limit 的比例显著上升；
- Group control backlog 与模型 Runner 竞争资源；
- 单一 Worker 进程的故障域或发布节奏影响 SLO；
- Codex App Server 的远程传输和动态 Tool 接口转为生产支持，并形成明确兼容价值。

在触发条件出现前，优先让现有 durable 内核更清晰、更可诊断、更容易证明正确，而不是扩张协议和部署形态。
