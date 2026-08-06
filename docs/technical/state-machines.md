# 状态机与推进契约

本文是 Agent-native v2 状态、合法迁移、推进者和恢复语义的技术权威；UAT 与生产 Profile 使用同一领域状态机。字段与约束见[数据库契约](database-schema.md)，外部命令见[API 与 SSE](api-sse.md)及 [OpenAPI 3.1](openapi-v1.yaml)。

## 1. 通用规则

- 所有迁移使用 `WHERE id=? AND version=? AND status IN (...)` 比较并交换；成功递增 version；
- 领域状态、ExecutionEvent 和必要 Outbox 在同一事务提交；
- 条件更新 0 行时重读：目标状态已达到则幂等成功，其他合法事实返回当前状态，不兼容命令返回 409；
- 终态不可复活。重试创建新的 Runner、Attempt、Invocation 或 Run；
- 取消先记录 intent，再停止可停止工作；已经确认的真实结果不被改写；
- 迟到结果只追加 Attempt/诊断事件，不推进终态聚合；
- 模型文本不能直接写 lifecycle、授权、verified 或 completion 状态；
- 每个事件必须有稳定 cause/dedupe key，至少一次投递不得造成重复副作用。

## 2. Agent 与 AgentVersion

Agent：`draft → published ↔ inactive`。

AgentVersion：可编辑 `draft`；publish 时复制为不可变 `published`；`published → deprecated`；`draft_test_snapshot` 创建即不可变。

其他 Registry 版本的准入状态：

```text
SkillVersion: uploaded → published | quarantined
              published → deprecated | quarantined
              deprecated → quarantined
PolicyVersion: published → deprecated
SandboxProfileVersion: published → deprecated
```

`deprecated` 和 `quarantined` 都是不可逆状态终点；恢复使用必须发布新版本。依赖版本的 `deprecated` 只阻止新 AgentVersion 选择/发布，不改变已发布 AgentVersion 的精确依赖；AgentVersion 自身 `deprecated` 后不得解析到新 Run/Invocation，已创建工作仍按快照恢复；SkillVersion `quarantined` 在新 Runner 启动前强制失败。上述迁移只能改变状态、治理 CAS 版本和审计元数据，不能改变 payload、依赖或 `content_hash`。ModelVersion 无独立可变状态；其新准入由 Model logical status/current pointer 与 ConformanceRun 共同决定。

迁移发起者也是固定的：新 Agent/Skill 版本发布事务 CAS 更新 logical current pointer，并可在同事务把旧 current 从 published 迁移为 deprecated；Policy/Sandbox 由签名 GitOps reconcile 执行等价操作；Skill quarantine 只能由安全评估服务或有权管理命令依据精确 assessment 事实推进。所有迁移均写 PlatformAuditEvent，不从 UI 文字或扫描器临时返回值直接改状态。

发布事务必须：

1. 锁 Agent 和 draft revision；
2. 校验 Model、Tool、Policy、Sandbox；
3. 把每个逻辑 Skill 解析为精确 `skill_version_id + checksum`；
4. 复制不可变标量与关系，计算 content hash；
5. 生成 AgentCard 并 CAS 更新 current pointer；
6. 写 PlatformAuditEvent。

任一步失败全部回滚。Skill 以后发布新版本不迁移已发布 AgentVersion。停用只阻止新的 Run/Invocation 解析，不取消已固定执行。

## 3. UserSession 与 Plan 权限

Session：`active → closed → archived`。页面关闭、用户离线和 Run 完成不迁移 Session。

Permission mode：`direct ↔ plan_readonly`，由用户显式进入/退出 Plan 或批准实施改变：

| 迁移 | 前置 | 同事务事实 |
| --- | --- | --- |
| direct → plan_readonly | Session owner 发起 Plan | permission revision++、`plan.permission_entered` |
| plan_readonly → direct | owner 批准精确计划实施或退出 Plan | Approval 终态、revision++、`plan.implementation_approved` 或 `plan.permission_exited`、恢复 WorkItem Outbox |

`plan_readonly` 只改变后续 CapabilitySnapshot：阻止写文件、执行和外部副作用，仍允许读取、澄清、WorkingPlan 和受控研究/委派。它不冻结 DAG，不创建 PlanExecution lifecycle。Approval 绑定 plan revision 与 subject hash；计划变更后旧批准不可复用。

## 4. Run：唯一生命周期权威

状态：

```text
active
waiting_user | waiting_approval | waiting_tool | waiting_agent
completed | cancelled | failed
```

`completed/cancelled/failed` 为终态。Plan 和 Group 不保存镜像 lifecycle/outcome。

| 迁移 | 推进者与机器前置 | Event / Outbox |
| --- | --- | --- |
| create → active | API/Runtime；首 Message、WorkItem、queued Runner 已原子创建 | `run.created` + `runtime.runner` |
| active → waiting_* | Coordinator；无可运行 Runner，且存在对应 pending action | `run.waiting`；等待对象负责 resume |
| waiting_* → active | action/Invocation/Tool 已结算，至少一个 WorkItem 可恢复 | `run.resumed` + runner/control Outbox |
| 非终态且 cancellation_requested_at 为空 → 记录取消 intent | 用户取消命令；CAS 写时间并开始向 Runner/Attempt 传播，不立即伪造终态 | `run.cancellation_requested` + control Outbox |
| active/waiting → completed | 根交付消息已提交，且结构检查无 pending；Group 另满足 delivered summary epoch | `run.completed` |
| 非终态 → cancelled | 用户取消 intent 已传播，所有必须结算的本地项已停止/记录 unknown | `run.cancelled` |
| 非终态 → failed | Harness/协议/预算失败且无法安全继续或交付 | `run.failed` |

Run 状态由 WorkItem、Invocation、Clarification、Approval 和 Tool 的结构事实计算。普通 Assistant final 只结束当前 Turn；根 Run 只有在 final 已交付且无 pending 时完成。业务上部分完成或拒绝可以用 `completed + terminal_reason` 和最终消息表达；`failed` 保留给平台无法形成安全交付的执行失败。

同一时刻存在多类等待时，Run 用稳定优先级展示 `waiting_user > waiting_approval > waiting_agent > waiting_tool`，完整 pending 列表由 RequiredAction/WorkItem API 返回，不能依赖单一 wait_reason 恢复。

`waiting_resource` 是 WorkItem 内部调度状态，不是 Run 状态。若只有 Workspace lease、Sandbox capacity 或其他自动可恢复资源等待，Run 保持 `active`，由资源/调度指标展示；资源超过硬 deadline 时 WorkItem 转 `timed_out|failed`，再按根工作是否仍可安全交付结算 Run。不得伪装成需要用户操作的 `waiting_user`。

## 5. WorkItem 与 Runner

WorkItem：

```text
queued → running
running → waiting_user | waiting_approval | waiting_tool | waiting_agent | waiting_resource
waiting_* → queued
queued/running/waiting_* → succeeded | failed | cancelled | timed_out | blocked
```

- 创建 queued WorkItem 时同时创建 queued Runner 与 Outbox，除非它明确在等待已有 action；
- Runner claim 后以 CAS 把 WorkItem 置 running；
- 创建持久等待对象、safe Checkpoint 并清除 active runner 后，WorkItem 才能进入 waiting；
- 等待满足时 execution_epoch++，创建新 Runner；旧 Runner 不复活；
- AgentInvocation 的目标 WorkItem 终态映射到 Invocation 终态并唤醒调用方；
- `blocked` 表示依赖已经终态且当前工作无法继续，不表示全 Run 自动失败。

Runner：`queued → running → succeeded|failed|cancelled|lost`。

- Worker 在短事务中 claim queued Runner，写 lease owner/token/expiry 并 ack 对应 Outbox；长模型循环不持有 Outbox claim；
- heartbeat 只续当前 lease；所有 Message、Checkpoint、Invocation 和结算写入验证 work item epoch 与 lease token；
- lease 过期由 Reconciler 把 Runner 置 lost。存在安全 Checkpoint 且 WorkItem 非终态时创建新 epoch Runner，否则 WorkItem failed；
- Runner succeeded 表示本执行段安全结束，不等于 Run 业务成功。

## 6. AgentTurn 与 ProviderAttempt

AgentTurn：`started → waiting_actions|completed|failed`。

- Provider 返回 Tool Call：持久化 Invocation 后进入 waiting_actions；
- 返回普通 Assistant final 且无 Tool Call：提交 Message 后进入 completed；
- 协议不可修复或 transport budget 耗尽：failed；
- Tool 结果回来后在同 WorkItem 的新 Turn 或恢复 Runner 中继续，不改写旧 Turn。

ProviderAttempt：`queued → running → succeeded|failed|timed_out|outcome_unknown`。仅在未收到 payload、可证明无模型侧副作用且重试预算允许时创建新 Attempt。已有 continuation/reference 时按 Provider 规范恢复；不得把 transport retry 当成新 Agent 决策。

## 7. ToolInvocation 与 ToolAttempt

ToolInvocation：

```text
created
→ waiting_confirmation | waiting_resource | queued
→ running
→ succeeded | failed | rejected | cancelled | outcome_unknown
```

- 创建时校验 Tool 在 CapabilitySnapshot、参数 schema、operation key 和 arguments hash；不合法请求以 rejected/failed 明确返回模型；
- `confirmation_policy=per_call` 时先创建 Approval，WorkItem 持久等待；`session_grant` 只有命中仍有效且覆盖精确调用范围的 Grant 才可直接 queued，否则同样创建 Approval；`never` 直接 queued；批准或执行前再次校验 tool、arguments、principal、permission revision、当前策略和 Grant 状态；
- Workspace lock 未取得时进入 waiting_resource，不占 Runner；
- ToolAttempt 承载物理重试：`queued → running → succeeded|failed|timed_out|cancelled|outcome_unknown`；
- 确认无副作用或 Tool 声明可按 operation key 幂等时才可重试；可能已发生外部副作用则 outcome_unknown，禁止盲目重放；
- Tool 后续停用阻止尚未 started 的执行；已 running 的真实结果照常记录。

## 8. AgentInvocation

状态：`created → queued → running → succeeded|failed|cancelled|timed_out|rejected`。

1. 调用方 Runner 创建 Invocation，并校验原始主体、模式、Collaboration Policy、目标可见性与 Group membership；
2. 校验失败也持久化 rejected 与安全 reason，不创建目标 WorkItem；
3. 成功时固定目标 AgentVersion，原子创建目标 WorkItem、Runner 和 Outbox，Invocation 进入 queued；
4. 目标首 Runner claim 后进入 running；
5. 目标普通 final 路由到 caller/direct/group，目标 WorkItem 终态并原子结算 Invocation；
6. 调用方等待的一批 Invocation 全部终态后创建新的调用方 Runner；
7. 调用方已终态时迟到结果只记录，不唤醒或复活 Run。

Chat、Plan、Group 和未来远程 Gateway 共享这一状态机；传输方式不能分叉领域语义。

## 9. Clarification 与 Approval

Clarification：`open → answered|cancelled`。只有 target user 可回答。回答事务创建 user Message、结算 Clarification、写 Event 和 resume Outbox。相同幂等请求返回原答案，不同答案返回 409。

Approval：`pending → approved|rejected|cancelled`。请求 AuthContext 的 `tenant_id + account_id` 必须与 Approval 的 `tenant_id + target_account_id` 相同；审批前重新核对 approval subject hash、资源版本、permission/credential/account authorization revision 和策略。Web/生产身份来自已验证账号；第一阶段 Agent API 的 account 由 raw userId Header 解析，因此 API Key 持有者能冒充目标用户。批准不是永久授权，只适用于精确业务 subject。Run 取消时未决动作进入 cancelled。

## 10. GroupSession、GroupRun 与自然汇总

GroupSession：`open → closed → archived`。`busy/idle` 从关联 Run 和 Invocation 派生。关闭会给全部非终态 Run 写取消命令；历史消息、Membership 和 Artifact 保留。

GroupMembership：`invited → joined|declined|cancelled`；`joined → left|removed`。第一阶段 `fixed_for_run` 下，成员变更只影响未来 GroupRun；当前 member snapshot 不热切换。

固定本轮策略下的图外邀请在授权检查阶段即拒绝，不创建 Membership；写 `group.membership_invitation_rejected` 安全 Event，并把稳定 reason 返回发起 Agent。它不是 `invited→declined`，因为目标从未进入候选 Membership。

GroupRun 没有独立状态。它与 Run 同事务一对一创建，之后只迁移 Run。

### Quiescence 与最终唤醒

每个可能减少 pending work 的事务都追加唯一 `evaluate_group_quiescence` 命令。Coordinator 在锁内检查：

```text
active/queued Runner = 0
non-terminal WorkItem or Invocation = 0（人类/Tool 等待按 Run waiting 处理）
pending Clarification / Approval = 0
explicitly assigned in_progress SharedTask = 0
Run cancellation intent = false
```

- 条件不满足：只按事实更新 Run active/waiting；
- 条件满足且不存在 queued/running SummaryRequest：锁内 `summary_epoch++`，为新 epoch 创建唯一 GroupSummaryRequest 和 Reporter `group_summary` WorkItem/Runner，写 `group.summary_requested`；
- Reporter Turn 创建新 Action：SummaryRequest 保持 running，Reporter WorkItem 按普通规则等待；Action 收敛后在同一 epoch 恢复该 WorkItem；
- Reporter 交付前若出现与当前汇总无因果关系的新用户/成员工作：当前 SummaryRequest=superseded；待全部新工作收敛后创建下一 epoch；
- Reporter 提交 final 后再次结构检查。无 pending 时同事务把 SummaryRequest=delivered、`delivered_summary_epoch=summary_epoch`、Run=completed，并写 `group.summary_delivered/run.completed`；
- 同一 `(group_run_id,summary_epoch)` 唯一，Worker 重启和重复事件不会重复最终唤醒；
- Reporter 不可用/连续执行失败：当前 SummaryRequest=`failed`、Run=`waiting_user`，创建选择 Reporter 的 Clarification；用户只能选择本轮 available member。回答事务 CAS 更新 `reporter_agent_id`、结算 Clarification 并追加 quiescence 命令；再次空闲时分配新 epoch/新 SummaryRequest，不复活失败的 Reporter WorkItem；
- 取消、硬预算或无法恢复的 Harness failure：Runtime 写机器状态摘要后直接结算 Run，不强制模型汇总。

Reporter 没有额外数据、Tool、审批或调度权限。它只是本轮最终交付责任人，不是 Supervisor。

## 11. SharedTask

`pending → in_progress → completed|blocked|cancelled`；pending 也可 cancelled，blocked 可回到 in_progress（显式新事实和 CAS）。

SharedTask 不自动调度 Agent。只有 assignee 明确且 status=in_progress 时进入 Group quiescence 的 pending 判断；无 assignee 的 pending 项只是白板记录，不能让 Group 永久等待。

## 12. NotificationDelivery（启用 Channel 时）

```text
pending → running → succeeded
                  → failed
                  → outcome_unknown
running → pending          # 仅安全可重试，设置 next_attempt_at
pending|running → dead     # 重试/租约预算耗尽
```

- 创建 Delivery 时同事务写 `notification_delivery.created` 与 `runtime.delivery` Outbox；
- Worker 以 lease/fencing 创建新 DeliveryAttempt，同一 Delivery 的 `provider_idempotency_key` 不变；
- 只有 Provider 明确拒绝/失败且错误不可重试才进入 failed；支持幂等或状态查询时先用原 key 对账；
- 请求可能已送达、又无法幂等查询时进入 outcome_unknown，绝不自动创建新业务投递；
- succeeded/failed/dead/outcome_unknown 均为终态，对应写稳定 Event；迟到回执只记 `late_result.observed` 和 Attempt 诊断，不复活 Delivery；
- Delivery 结果不改变原 Run 终态；Web/API 仍可从可靠 Event 查询已提交结果。

## 13. Artifact

Artifact 的迁移由 status 与 reason 共同约束：

```text
publishing → published
publishing → unavailable(publish_failed) → deleted
published → unavailable(deletion_pending) → published | deleted
published → unavailable(retention_expired) → deleted
published → unavailable(storage_fault|checksum_mismatch) → published
unavailable(storage_fault|checksum_mismatch) → unavailable(deletion_pending) → deleted
```

用户/策略只创建删除意图，实际可见性撤销和物理删除只由受审计 Retention Service 推进；Legal Hold 命中时拒绝 `deletion_pending|retention_expired` 迁移。用户删除进入 7 天恢复窗口，owner 在本体尚未清除且无强制 Retention 时可以用 CAS 取消，恢复为 published；到期后不可恢复。storage/checksum 故障只有 Storage + Security Owner 从受信副本恢复与原 checksum 完全相同的字节、重新验证对象锁后才能回 published，并写 `artifact.restored`；不能用新内容“修复”。`publish_failed` 从未对外发布，只允许 Reconciler 清理为 deleted。`unavailable` 的语义必须由 `availability_reason` 判断，不能仅凭状态推测。

Publish 流程：

1. 校验 ExecutionPrincipal 对源逻辑根和目标 visibility 的权限；
2. 数据库事务分配 artifact_id 和平台只写 immutable storage key，创建 Artifact=`publishing`；
3. 在 Workspace lease 中读取字节、计算 checksum，写入临时对象/文件并读回复核；
4. 以 create-if-absent/原子 rename 生成不可覆盖的最终存储对象；
5. 数据库 CAS 核对 publishing 和 checksum，同事务写 Artifact=`published` 与 `artifact.published` Event，此时才对外可见；
6. 任一崩溃窗口由 Reconciler 按 artifact_id/checksum 幂等完成或标记 `unavailable`；绝不让缺少最终字节的记录进入 published。

published 的 storage key、checksum、bytes metadata 和 provenance 不可修改。恢复只能找回同 checksum 字节；更新内容必须新建 Artifact 并关联 parent。

## 14. Outbox

`pending → claimed → completed`；`claimed → retry_wait → claimed`；任一可恢复状态在预算耗尽后 `→ dead`。

- claim 使用短 lease；业务 Worker 启动后及时 ack 或以新的 cause 命令推进；
- `topic + dedupe_key` 唯一；重复 command 是幂等成功；
- lease 过期只允许重新 claim 同一 command，不重置领域状态；
- dead 必须告警并可通过运维命令创建新 command，不能手改回 pending；
- Redis 不参与状态迁移，仅传 provisional token 和低延迟提示。

## 15. 终态与迟到事实矩阵

| 对象已终态 | 迟到输入 | 处理 |
| --- | --- | --- |
| Run | Tool/Agent/Provider 结果 | 记录 `late_result.observed`，不改 Run、不再交付第二次终态 |
| WorkItem/Runner | 旧 lease 写入 | fencing reject + 安全诊断 |
| Invocation | Provider/remote 回执 | 新 Attempt 诊断，不复活 Invocation |
| Clarification/Approval | 第二个决定 | 相同请求返回原结果；不同决定 409 |
| Artifact | 新字节 | 拒绝覆盖；创建新 Artifact |
| Group summary epoch | 重复唤醒 | 唯一键命中，返回既有 SummaryRequest |

以上规则必须有并发测试，不得只靠应用 happy path。

## 16. 辅助与治理状态

下列状态虽不直接决定 Run lifecycle，仍是实施必须遵守的唯一迁移集。所有管理迁移要求 CAS + Idempotency-Key + PlatformAuditEvent；时间驱动迁移使用数据库时间和幂等 Maintenance 命令。

| 对象 | 合法迁移 | 推进者/关键语义 |
| --- | --- | --- |
| Tenant | `active ↔ suspended` | 安全/平台管理；suspended 阻止新请求和副作用，不删历史 |
| PlatformAccount | `active ↔ suspended`；`active|suspended → closed` | 身份管理；每次迁移同时递增 authorization_revision 并撤销 BrowserAuthSession；closed 终态，不可复开 |
| ExternalIdentity | `active → revoked` | 身份管理；revoked 终态，重新绑定创建新行 |
| TenantIdentityProvider | `active ↔ inactive` | 签名 GitOps/安全管理；inactive 后拒绝该 issuer 的新认证，不 fallback |
| WorkloadClientPolicy | `active → revoked` | 签名 GitOps/安全管理；重新授权创建新行与新 credential revision |
| OidcLoginTransaction | `pending → consumed|expired` | callback CAS 一次性 consume；数据库时间到期，terminal 不可复用 |
| BrowserAuthSession | `active → revoked|expired` | 登出/身份权限撤销或数据库时间到期；不得复活，重新登录创建新行 |
| AgentApiKey | `active → revoked|expired` | 撤销命令/到期 Maintenance；轮换原子撤销旧 Key 并创建新 Key |
| HttpIdempotencyRecord | `in_progress → completed|failed` | 请求事务；terminal 后同 key/hash 重建稳定响应，不再执行命令 |
| Model/Skill/PolicyDefinition/SandboxProfile | `active ↔ inactive` | Registry/GitOps；inactive 阻止新准入，不改写版本内容 |
| ConformanceRun | `queued → running → passed|failed` | Conformance Worker + lease；terminal 不可覆盖，重测创建新行 |
| SkillAssessment / Decision | 创建即 `trusted|unknown|quarantined` / `approved|rejected` 终态 | Scanner/授权安全管理员；新规则创建新 assessment revision |
| WorkingPlan | `active → superseded|closed` | 新 head 产生时 supersede 旧 head；Run 终态后 close；PlanUpdate 始终只追加 |
| SessionGrant | `active → revoked|expired` | 用户/安全命令、账号 authorization revision 变化或 Maintenance；不可扩大范围或复活 |
| MemoryFact | `active → deleted` | 修改内容不改 Fact 状态，而是追加 MemoryVersion 并 CAS current pointer；deleted 不召回 |
| HistorySummary | `active → superseded` | 新摘要在同 scope 中取代旧 head；旧内容保留作来源证据 |
| VerificationResult | 创建即 `passed|failed|insufficient_evidence` 终态 | 重新验证单调分配新 `verification_epoch`，保留 rule version 和类型化 evidence refs，不覆盖旧证据 |
| SessionSandbox | `provisioning → ready|failed`；`ready|failed → retained → deleted` | Sandbox/Retention；实例丢失不改 SessionSandbox 存储身份 |
| RunnerSandboxInstance | `provisioning → running|failed|lost`；`running → stopped|failed|lost` | Sandbox Broker/lease；stopped/failed/lost 终态，恢复创建新实例 |
| WorkspaceLockRequest | `queued → granted|cancelled|expired`；`granted → released|expired` | Lock Coordinator；过期后旧 lease/fencing token 永久无效 |
| ChannelPlugin | `active ↔ inactive` | 签名 reconcile；未通过当前 Conformance 不得 active |
| ChannelAccount | `active ↔ error`；`active|error → revoked` | Provider/Owner；revoked 终态，不再发送/接收 |
| AgentChannelBinding | `active → inactive` | Owner/Admin；新绑定创建新行 |
| ChannelIdentityBinding | `active → revoked` | 已验证身份管理；重绑创建新行，不改历史 account |
| ChannelConversationBinding | `active → closed` | 明确新会话/解绑；closed 不再接收新 Message |

未出现在本文的新 status 不能由实现自行添加；必须先同步数据库、OpenAPI/内部接口、Event 和并发/恢复测试。
