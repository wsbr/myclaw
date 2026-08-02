# Context、History 与 Memory 实现

本文是第一阶段四类 Policy接口、默认参数、摘要、Group投影、Memory生命周期和可解释记录的唯一实现级定义。

## 1. 策略接口

```python
class ContextPolicy(Protocol):
    async def assemble(self, request: ContextRequest) -> ContextSection: ...

class HistoryPolicy(Protocol):
    async def select(self, request: HistoryRequest) -> HistorySelection: ...

class MemoryWritePolicy(Protocol):
    async def extract(self, request: MemoryWriteRequest) -> list[MemoryCandidate]: ...

class MemoryRecallPolicy(Protocol):
    async def recall(self, request: MemoryRecallRequest) -> MemorySelection: ...
```

Request必须含 userId、agent_id、session/run/work_item/runner、模式、group node、当前输入、固定 policy/parameter version与可见范围上界。Result必须含内容、source IDs、裁剪/排序原因、token estimate和参数版本。实现只能通过 `VisibleRecordRepository`读取已授权投影，不能自行扩大查询。

## 2. 默认参数位置

代码中每个 Policy提供版本化 schema与安全默认；数据库 `policy_definition.default_parameters + parameter_version` 是部署生效配置。管理员可改平台默认并递增 parameter_version，Agent只选 key。Runner启动固定合并后的参数快照。环境变量只允许设置部署级硬上限，不能形成隐藏 Agent覆盖。

建议首批默认：History总预算 24k tokens、recent保留最多 80条；summary触发早期可见历史超过 12k，摘要预算 2k、recent 8k；Memory recall最多 20条/4k；Context业务元数据最多 8k。实际值由Registry seed可调整，不进入Agent页面。

## 3. `context.standard`

按固定小节生成：ExecutionPrincipal摘要（不含秘密）、Session/模式、WorkItem目标与状态、Plan冻结节点/依赖或Group目标/节点目录、Checkpoint恢复信息、当前可用Artifact引用、当前定向输入。它不选择历史消息或长期Memory，不注入其他Agent私有配置。

## 4. History

### `history.recent`

从直接可见 Message按 `(created_at,id)`倒序取到Token预算，再恢复正序。当前输入、最近未决Clarification/Confirmation和其因果消息不可被裁掉。Tool原始大结果以摘要与Artifact引用代替。

### `history.summary_recent`

读取同投影最新 ready `history_summary`，验证其 `source_hash/through_sequence`仍是当前可见前缀；再取之后的最近消息。若无可用摘要，立即按 `history.recent`继续，并写异步摘要命令。摘要失败记录事件，下次仍可重试，不切模型、不扩大范围。

摘要Worker读取一个固定的 `through_message_id/event_sequence`快照，生成后再次确认投影前缀hash；不匹配则标 superseded并重排。摘要是压缩缓存，不是Memory、Message或事实源；用户修改/删除来源Memory不影响历史摘要，但消息redact后必须使覆盖它的摘要失效。

## 5. Group可见投影

投影键为 `group_session_id + group_node_id`。查询允许：Group目标/确认图/整体状态；本节点发送或接收的消息；用户定向消息；本节点Clarification；本节点WorkItem；本节点参与的AgentInvocation；明确发送给本节点的Artifact；本节点ToolInvocation。Group公共目录信息可见但其他节点工作正文不可自动可见。

每个节点独立维护 HistorySummary；严禁一份全群摘要自动注入所有Agent。`search_group_history`使用专门共享查询视图，结果作为当前Runner Tool结果，不写入HistorySummary的历史前缀，也不自动写Memory。

## 6. Memory Write

- `memory_write.none`：不创建命令。
- `memory_write.user_agent_facts`：只从用户明确表达/确认的稳定事实提取，scope=`user_agent`，键含 userId+agent_id。
- `memory_write.user_facts`：只提取适合跨Agent复用的用户稳定事实，scope=`user`；领域私密偏好若不适合跨Agent则丢弃。

Candidate必须含 `fact_key/content/source_message_ids/evidence_excerpt/confidence/scope/reason`。`fact_key` 先 Unicode normalize、trim、lowercase 并由策略映射到稳定领域键；拒绝模型推测、临时计算、内部分析、未经用户确认Tool结果和敏感凭证。Memory Worker先验证来源属于同userId，再 canonicalize 与 normalized_hash 去重。

逻辑唯一键为：user scope=`userId + user + fact_key`；user_agent scope=`userId + user_agent + agent_id + fact_key`。物理上先定位唯一 MemoryHead，再锁 Head：

1. Candidate 记录生成时看到的 `observed_head_version` 和来源消息；
2. Worker `SELECT memory_head ... FOR UPDATE`，不存在时按唯一逻辑键插入，并在并发 duplicate-key 时重读；
3. 同内容只补充来源并把 Candidate accepted，不无界追加 Memory 版本；
4. 新内容兼容时先把旧 active 标 superseded，再插入新 active，二者与 Head version+1 同事务；generated active_head_id UNIQUE 是最后防线；
5. 内容冲突且证据不足时 Candidate rejected，不改 active；
6. `last_user_edited_at` 晚于 Candidate 创建，或 observed_head_version 已变化且不能证明兼容时，模型 Candidate rejected，用户编辑优先。

模型写入成功、拒绝、失败在来源 Session 写 `memory.updated/candidate_rejected/write_failed` ExecutionEvent，payload 只含 Memory/Candidate ID 和安全原因。用户通过 Memory API 编辑或删除没有所属 Session：创建不可变新版本或标记 deleted，并写不含内容的 PlatformAuditEvent。异步失败不改 Run。

## 7. Memory Recall

- `memory_recall.none`：空结果。
- `memory_recall.user_agent`：只查询 active `(userId,agent_id,scope=user_agent)`。
- `memory_recall.user_combined`：查询上述集合与 `(userId,scope=user)`，先Agent专属、再用户通用；同fact_key专属覆盖通用。

第一阶段使用关系过滤 + 关键词/最近性/显式fact_key评分即可，向量索引不是验收前提。排序必须稳定并受Token/数量上限。`agent`跨用户scope仅保留Schema，首批策略不得读写。

用户可列出、按 scope/agent 筛选、查看来源、编辑和删除自己的 Memory。编辑锁 Head 并创建 `source_kind=user_edit` 新版本；删除锁 Head，把 active 标 `deleted` 并递增 Head version，提交后所有召回查询立即排除。并发模型 Candidate 按上述 user-edit 优先规则拒绝或重新合并。已经启动 Runner 的 `runner_memory_snapshot` 不修改，后续 Runner 不再召回已删版本。Builder/Admin 普通接口不能读取用户 Memory。

## 8. Prompt与Trace可解释性

Runner保存 `memory_recall_manifest`：policy key/version、query摘要、候选数、选中memory_id、score、scope、裁剪原因；Prompt section只记录来源ID与checksum，敏感全文按部署Trace策略脱敏。Trace的 `memory_recall` span引用同manifest并记录耗时，不能成为召回事实源。

History manifest记录选中message IDs、summary_id、被裁剪数量、token估算和Group投影键。由此每个Runner可回答使用了哪些历史/Memory及原因，而不记录模型私有推理。
