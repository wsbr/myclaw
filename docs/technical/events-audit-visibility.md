# V1 执行事件、平台审计与可见性（历史实现参考）

> **历史 V1 实现参考，非目标设计。** Event/Audit 分层只在被当前方案确认的范围内复用；Turn、WorkingPlan/Approval、Group Membership/Message 和 Verification 以[综合技术方案](phase-one-agent-native-delivery.md)为准。

本文记录V1 `ExecutionEvent`、`PlatformAuditEvent`、事件幂等与用户/Agent查询投影基线。物理字段见[数据库Schema](database-schema.md)，状态迁移见[状态机](state-machines.md)，HTTP/SSE外形见[API与SSE](api-sse.md)。

## 1. 两类事件的边界

`execution_event` 只记录已有 `UserSession` 中的可靠执行时间线。它必须有 `session_id + sequence`，适用于 Message、Run、Plan、Group Formation/GroupRun、WorkItem、Runner、Invocation、Clarification、Tool Confirmation、Artifact、Session 来源 NotificationDelivery，以及由这些执行产生的 Memory 写入结果。

`platform_audit_event` 记录不属于任何 Session 的控制面和安全操作，包括账号/角色、Agent/AgentVersion、API Key、Model、Platform Tool、Skill/SkillVersion、PolicyDefinition、SandboxProfile、ChannelAccount 与 Registry reconcile。它不含 `session_id`，不分配 Session sequence，不进入用户 Session SSE，也不得通过创建虚构 Session 来复用 ExecutionEvent。

同一操作只选择一个主事件账本：Session 执行迁移写 ExecutionEvent；平台资源迁移写 PlatformAuditEvent。若一次平台操作还触发异步工作，例如 Skill 发布后同步共享目录，则平台资源变化与审计事件同事务写入，并另写 OutboxCommand；审计事件本身不是队列触发器。Session 内的执行若被平台治理影响，例如 Tool 在调用前发现已停用，写 `tool.failed` ExecutionEvent；Tool 被管理员停用这一管理动作只写 PlatformAuditEvent。

边界按资源而不是名称判断：SandboxProfile 配置/启停是平台审计，某个 SessionSandbox 的 provisioning/failed/cleaned 是 ExecutionEvent；SkillVersion 发布/同步治理是平台审计，某个 Runner 因固定 checksum 缺失而失败是 ExecutionEvent。不得把同一管理迁移同时复制到两类账本。

| 迁移对象 | 事件账本 |
| --- | --- |
| UserSession、SessionSandbox、Message、Run、WorkItem、Runner、PlanExecution、GroupFormationProposal/Revision、GroupSession/GroupRun/Node、Invocation/Attempt、Clarification/Confirmation、Artifact、NotificationDelivery、Session来源Memory写入 | ExecutionEvent |
| PlatformAccount/Role、Agent/AgentVersion、AgentApiKey、Model、PlatformTool、Skill/SkillVersion、PolicyDefinition、SandboxProfile、ChannelAccount/Binding、Registry reconcile与maintenance治理 | PlatformAuditEvent |

ToolAttempt、NotificationAttempt 和迟到 callback 若属于 Session，诊断仍是 private ExecutionEvent；纯平台 maintenance job 没有 Session 时只写 PlatformAuditEvent。

## 2. ExecutionEvent 幂等

`entity_version` 是解释字段，不再参与唯一约束。每条事件必须带非空 `event_dedupe_key`：

- 状态迁移：`transition:<entity_type>:<entity_id>:<from_version>:<to_version>:<transition_name>`；
- 同一事务的附属事件：在 transition key 后追加稳定的 `effect_name`；
- 回调/Attempt/迟到结果：`cause:<cause_type>:<cause_id>:<event_type>`，其中 cause ID 是 provider event、Attempt、callback 或诊断记录的稳定 ID；
- HTTP 命令产生的非迁移事件：`command:<operation>:<idempotency_record_id>:<effect_name>`。

数据库唯一键为 `(session_id,event_dedupe_key)`。`transition_key` 可空，用于把同一领域迁移产生的多个事件关联起来，但不唯一；`caused_by_event_id` 表达事件因果。这样同一次迁移重试不会重复记账，而同一终态实体的不同 Attempt、回调和迟到结果可以分别记录。EventWriter 遇到 dedupe 冲突时读取并校验既有事件的 entity/cause，语义不同则报告 `event_dedupe_collision`，不得静默吞掉。

`platform_audit_event` 使用全局非空 `dedupe_key`：成功管理命令以 `audit:<operation>:<idempotency_record_id>` 生成，内部 reconcile 以 `audit:<job_run_id>:<resource_type>:<resource_id>:<action>` 生成。失败且未产生领域变更的管理请求可以在原事务回滚后单独写失败审计，dedupe key 使用 request ID；不得在审计事件中保存密码、Secret、credential_ref 的值或用户业务正文。

## 3. ExecutionEvent visibility

| 值 | 含义 | 用户/API | Group Agent |
| --- | --- | --- | --- |
| `user` | 面向 Session 所有者的安全执行事实 | 同 `user_id` 的 Web/Agent API 可见 | 只有同时列为 recipient 的节点可见，否则不进入 Agent 投影 |
| `group` | 已确认 Group 的共享、可查询记录 | Session 所有者可见 | Group 成员只能通过 `search_group_history` 查询；不自动进入 Context/History |
| `direct` | 只面向明确接收者的请求、结果或等待 | 仅当 user recipient 中包含 Session 所有者时可见 | 仅列在 recipient 表中的 GroupNode/Agent 可见 |
| `private` | Runtime 恢复、敏感 Tool、Prompt/Memory 诊断等内部事实 | 普通事件、Timeline、SSE 均不可见 | 仅与当前 WorkItem/GroupNode匹配的内部 Runtime repository 可读；不得由 `search_group_history` 返回 |

`direct` 的接收者不塞入 payload，而写 `execution_event_recipient(event_id, recipient_type, recipient_id)`；第一阶段 recipient_type 为 `user_identity | group_node | agent`。`user` 事件需要定向进入某节点 Prompt 时也必须有 recipient。`private` 事件用 `private_owner_type/private_owner_id` 标识 `work_item | group_node | runner | service`，仅服务身份在匹配执行范围内可读。

Session 所有者可以看到 `user`、`group`，以及明确发给自己的 `direct` 事件；“完整 Group 时间线”指全部安全共享 `group` 事件，不包含 Agent 私有 Tool 原文、Prompt、Memory、模型推理或节点间仅 direct 的敏感正文。必要时同一领域迁移分别产生一个安全 `user/group` 摘要事件和一个 `private` 诊断事件，两者使用不同 dedupe key。

Builder、Admin 和 Agent owner 不因角色或蓝图所有权获得用户事件。Admin 普通排障接口只读取平台审计、聚合状态和脱敏 Trace；第一阶段没有绕过投影读取用户事件 payload 的接口。

## 4. 统一 VisibleEventProjection

REST `/events`、SSE、Timeline、Group 页面、`VisibleRecordRepository` 和 `search_group_history` 必须调用同一 `VisibleEventProjection`，禁止各自拼接 visibility SQL：

```python
class VisibleEventProjection(Protocol):
    async def scan(self, principal, session_id, after_sequence, scan_limit, view) -> EventPage: ...
```

`view` 只能是 `session_user | group_node_direct | group_shared_search | runtime_private`。投影先以 `session_id + sequence` 扫描权威序列，再按 principal、visibility、recipient、Group 成员关系和实体所有权过滤；过滤发生在分页游标计算之前，但返回游标取“已经扫描到的最大 sequence”，而不是最后一条可见事件。

- Session 用户事件页返回 `items[]`、`scanned_through_sequence`、`has_more`；即使本页零条可见事件，游标仍推进。
- SSE 扫过只有不可见事件的区间时发送无领域 payload 的 `event: cursor`，`id` 为 scanned sequence，客户端只推进游标；后续可见事件仍保留真实 sequence，因此空洞合法。
- `VisibleRecordRepository` 的 `group_node_direct` 只返回本节点直接可见 Message/Event/Artifact，不返回普通 `group` 共享记录。
- `search_group_history` 使用 `group_shared_search`，可查询 `group` 事件和调用者本身有权读取的 `direct` 事件；不能借查询取得其他节点 direct/private 记录。
- 查询以调用开始时的 `max_sequence` 固定上界，分页 cursor 包含该上界和最后扫描 sequence。

客户端不得以 sequence 连续性判断丢事件。可靠性判断只使用服务端 `scanned_through_sequence`、当前 Session high watermark 与 event_id 去重。

## 5. Summary 与 payload 脱敏

事件生产者使用注册的 `EventPresentationPolicy(event_type, visibility)` 构造白名单 payload。`summary` 必须对该 visibility 的全部合法读者安全；payload 默认只含资源 ID、状态、结果类别、版本、Artifact 引用和受控错误码。以下内容禁止进入 `user/group/direct` payload：

- Agent Instructions、完整 Prompt、Memory 内容或召回 query；
- 模型内部推理与原始 Provider 请求；
- credential、Token、宿主机路径、未脱敏堆栈；
- 未确认可共享的 Tool 参数/结果、Workspace 文件正文；
- direct Clarification、AgentInvocation 的正文被复制到 group 事件。

敏感字段只保存在相应领域表或受限 Trace；面向用户需要状态时生成独立安全摘要。读取时再进行字段级权限裁剪，但不能依赖读取时脱敏来弥补生产者写入了不该共享的内容。

## 6. PlatformAuditEvent 访问与保留

- Admin 可通过管理 API 按时间、category、action、resource、actor 和 outcome 查询已脱敏平台审计。
- Builder 只能查询自己拥有的 Agent/Skill/API Key 的生命周期审计子集，changes 中平台秘密与其他账号信息被移除。
- 普通用户只能通过 `/accounts/me/audit-events` 查看自己的登录、userId、安全设置和 API Key 安全事件；不能使用全局审计 API。
- Service actor 事件记录稳定 service name/job run ID。source IP/User-Agent 只保存规范化后 hash 或安全摘要。

审计事件 append-only，成功治理事件至少保留 365 天，认证失败类按部署安全策略至少保留 180 天；法律或企业策略可延长。清理使用独立 maintenance job 并写一条不包含被清理内容的 retention 审计。PlatformAuditEvent 不通过用户 SSE 分发；管理页按游标轮询查询。
