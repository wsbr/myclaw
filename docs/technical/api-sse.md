# 第一阶段 API 与 SSE 契约

本文是第一阶段外部 HTTP 资源、请求响应、错误、幂等、分页、权限和事件续读的唯一实现级定义。基路径统一 `/api/v1`，JSON 使用 snake_case，时间使用 RFC3339 UTC。事件可见性必须调用 [统一投影](events-audit-visibility.md)，不得在路由中自行放宽。

## 1. 通用协议

- 状态命令使用 `Idempotency-Key`；资源编辑另带 `If-Match: "<version>"`。登录、只读查询和 connection-test 不要求幂等键；connection-test 不保存业务状态。
- 创建异步执行返回 `202`：`{resource,run,required_actions,links}`。创建普通管理资源返回 `201`，成功编辑/命令返回 `200`，逻辑删除可返回 `204`。
- 列表参数为 `limit<=100&cursor=`，可加文档明确的过滤字段；响应统一 `{items,next_cursor}`。cursor 是绑定 principal、过滤条件和稳定排序键的签名不透明值。
- 所有可编辑资源响应含 `version`；409 返回当前 version/state 和可重取链接。Published/冻结资源写入返回 `409 immutable_resource|graph_frozen`。
- 用户内容资源无权读取返回 404 防枚举；已知管理资源无权操作返回 403。Registry 列表仅 admin 可管理，builder/user 只通过专用 selection endpoint 取得可装配/可使用的安全投影。

错误格式：

```json
{"error":{"code":"version_conflict","message":"Resource changed","request_id":"uuid","retryable":false,"details":{"current_version":4,"current_state":"awaiting_confirmation"}}}
```

常用状态：400 validation、401 authentication、403 authorization、404 not_found、409 state/idempotency/version conflict、410 event_cursor_expired、422 domain_validation、429 limit、503 dependency_unavailable。`503 realtime_bridge_degraded` 只用于明确要求实时健康检查的接口；Run 创建和事件查询不能因 Redis 故障失败。

## 2. 认证、账号、角色与 API Key

- `POST /auth/login|refresh|logout`、`GET /auth/me`；Token 含 account_id、user_id、auth_revision。
- `GET /auth/enterprise/start|callback`；只接受 IdP 验证 userId。
- `POST /identity/preprovision`：Agent API Key，body `{"user_id":"..."}`，返回 `{account_id,user_id,created}`。
- `GET/PATCH /accounts/me`、`POST /accounts/me/change-user-id`；PATCH/变更要求 version、If-Match 和 Idempotency-Key。
- Admin：`GET/POST /admin/accounts`、`GET /admin/accounts/{id}`、`PATCH /admin/accounts/{id}/roles|status`。列表支持 status/role/user_id_prefix；角色请求 `{"roles":["user","builder"]}`，响应含 account version。
- `GET/POST /agents/{agent_id}/api-keys`、`POST .../{key_id}/rotate`、`DELETE .../{key_id}`。创建 body `name,caller_label,expires_at?`；明文 secret 只返回一次。owner builder/admin 可管理，普通 user 不可见。

Web 使用平台 Bearer Token。Agent API 使用绑定 Agent 的 Bearer Key，并以 `X-MyClaw-User-Id` 传可信 canonical userId；管理路径一律 403。API Key 撤销只阻止新请求，不隐式取消已提交 Run。

## 3. Agent 草稿、发布与版本

- `GET /agents?status=&visibility=&owner=&limit=&cursor=`；user 只见可用 enterprise 与自己的 private，builder 另见自己草稿，admin 见全部安全元数据。
- `POST /agents` body `{visibility,draft:{name,description,...}}`，201 返回 `{agent,draft}`；服务创建唯一 draft，`draft.version_no=null,draft_revision=1`。
- `GET /agents/{id}`、`GET/PATCH /agents/{id}/draft`。PATCH body 可含五区字段及 `tool_ids/skill_ids/bound_agent_ids`，响应含新的 draft_revision/version。
- `POST /agents/{id}/publish` body `{"expected_draft_revision":7}`。200 返回 `{agent:{status,version,current_published_version_id,draft_version_id},published_version:{id,version_no,content_hash},draft:{id,draft_revision,derived_from_version_id}}`。发布复制生成 immutable published，draft ID 保持不变并 rebase；失败 422 且整事务不改变指针。
- `POST /agents/{id}/deactivate|reactivate`，body `reason?`；200 返回 Agent。inactive 发布新版不自动 reactivate。
- `GET /agents/{id}/versions?publication_status=&cursor=`、`GET /agents/{id}/versions/{version_id}`；published/test snapshot 只读，test snapshot 不出现在普通列表。
- `POST /agents/{id}/draft/test-runs` body `{session_id?,content,expected_draft_revision}`；202 返回 draft_test Run，服务先复制 immutable draft_test_snapshot。仅 owner builder/admin，私有 Chat，不能 Plan/Group/AgentInvocation。

所有管理迁移写 PlatformAuditEvent，不进入 Session SSE。

## 4. Registry 集合

### Model

- `GET /models?provider=&enabled=&cursor=`、`GET /models/{id}`：admin 完整管理投影；`GET /agent-options/models` 给 builder 返回 enabled 的 id/name/capability 摘要。
- `POST /models`：admin，body `{key,provider,model_name,endpoint,credential_ref,default_parameters,capabilities,enabled}`，201 返回 secret-free Model/version；响应只含 `credential_configured=true` 与安全 label，不回显 credential_ref 值。
- `PATCH /models/{id}`：admin + If-Match/Idempotency-Key，可编辑 endpoint、credential_ref、参数、capabilities、enabled；`POST /models/{id}/enable|disable` 是显式状态命令。
- `POST /models/{id}/connection-test`：admin，200 `{ok,latency_ms,provider_error_code?}`，不得回显凭证；503 仅表示本次依赖不可用。

### Platform Tool

- `GET /platform-tools?enabled=&execution_mode=&confirmation_policy=&cursor=`、`GET /platform-tools/{id}`：admin；详情含 Schema、implementation/version、registration_hash、editable_config_schema、usage_count、version/config_version，platform_config 中的 secret 字段只返回 configured 标记。
- `GET /agent-options/platform-tools`：builder，只返回可选择 Tool 的 id/key/name/description/execution_mode/current enabled。
- `PATCH /platform-tools/{id}`：admin，可编辑 `display_name,description,enabled,confirmation_policy,platform_config`，不可改 key/Schema/implementation/mode。200 返回新 config_version；无效字段 422 `code_owned_field`。
- `POST /platform-tools/reconcile`：admin，返回 `{created,updated,registration_missing,audit_event_id}`；`POST /platform-tools/{id}/connection-test` 返回安全测试结果。

### Skill

- `GET /skills?owner=&status=&cursor=`、`GET /skills/{id}`；builder 见自己维护项和可选共享项，admin 见全部。
- `POST /skills` multipart 创建逻辑 Skill+首版本；`POST /skills/{id}/versions` 上传新包，202 返回 validating version。
- `GET /skills/{id}/versions?status=&cursor=`、`POST /skills/{id}/versions/{vid}/publish`、`POST /skills/{id}/sync`。发布/同步要求幂等键；响应含 checksum、sync_status、version。
- `GET /agent-options/skills` 返回当前可装配逻辑 Skill 摘要，不泄露包宿主路径。

### Policy 与 Sandbox

- `GET /policy-definitions?policy_type=&enabled=&cursor=`、`GET /policy-definitions/{id}`、`PATCH /policy-definitions/{id}`：admin；PATCH 仅 name/description/default_parameters/enabled 并递增 parameter_version。`GET /agent-options/policy-definitions?policy_type=` 给 builder 返回 enabled 选项。
- `GET /sandbox-profiles?enabled=&cursor=`、`GET /sandbox-profiles/{id}`、`PATCH /sandbox-profiles/{id}`：admin；首批只能治理已注册 local 的 description/config/enabled。`GET /agent-options/sandbox-profiles` 给 builder 返回 enabled 安全摘要。

所有 Registry 列表均按稳定 `(key,id)` 或 `(created_at,id)` 排序，支持 cursor；创建/编辑/启停写 PlatformAuditEvent。普通 user 无 Registry 管理接口。

## 5. Session、Message、Run 与权威状态

- `GET/POST /sessions`。创建 body `{entry_agent_id,origin_type}`；Web 只能 origin_type=web，Agent Key 只能 api，Channel 由入站服务创建。201 响应含 session/version/session_sandbox。
- `GET/DELETE /sessions/{id}`、`GET /sessions/{id}/messages?scope=&cursor=`。
- `POST /sessions/{id}/messages` body `{content,mode:"chat"}`；同事务创建 Message/Run/WorkItem/Runner，202。
- `GET /runs/{id}`、`GET /runs/{id}/work-items`、`GET /work-items/{id}`、`GET /runners/{id}`。
- `POST /runs/{id}/cancel` body `{reason}`，If-Match 指 Run version；`POST /runs/{id}/retry-work-items/{work_item_id}` 只在状态机允许且创建新 Runner 时成功。

Run 响应是执行权威：`{id,mode,execution_kind,lifecycle,outcome,wait_reason,version,cancellation_requested_at}`。completed 必有 outcome，cancelled outcome 为 null。Plan/GroupRun 响应嵌入同一 Run 对象，不返回独立镜像 lifecycle。

Chat 202 示例：

```json
{"message":{"id":"m1"},"run":{"id":"r1","lifecycle":"running","outcome":null,"version":1},"required_actions":[],"links":{"events":"/api/v1/sessions/s1/events/stream"}}
```

## 6. Plan

- `POST /sessions/{id}/plans` body `{goal}`，202 返回 proposal generation Run；完成后得到 PlanExecution draft。
- `GET /plans/{id}` 返回 `{id,definition_status,graph_revision,version,run:{lifecycle,outcome,version},...}`。
- `PATCH /plans/{id}` body `{goal,deliverable,nodes[],dependencies[],expected_graph_revision}`；If-Match。200 返回完整关系化 graph 与新 revision；已 frozen 409。
- `POST /plans/{id}/revise` body `{instruction,expected_graph_revision}`，202 创建生成工作，不自动确认。
- `POST /plans/{id}/confirm` body `{expected_graph_revision}`；200 返回 `definition_status=frozen,frozen_hash,agent_version_ids,run`。
- `POST /plans/{id}/cancel`、`GET /plans/{id}/graph`、`GET /plans/{id}/work-items`。

confirm 同事务把 Plan definition frozen 与唯一 Run waiting→running。后续 API 只读取 Run lifecycle/outcome。

## 7. Group Formation 与 Group

该组 API 的资源对象名为 `GroupFormationProposal`，URL 使用复数 `group-formations`。

- `POST /sessions/{id}/group-formations` body `{goal}`，202 返回 `{proposal:{id,status:"generating",version},group_session:{id,status:"forming",version},generation_run}`；forming GroupSession 不可接收消息。
- `GET /group-formations/{id}` 返回 current revision 摘要、version、generation Run 和 links。
- `GET /group-formations/{id}/revisions?cursor=`、`GET /group-formations/{id}/revisions/{revision_no}` 返回：

```json
{"revision_no":3,"goal":"...","bootstrap":{"context":"...","sources":[{"type":"message","id":"m1"}]},"participants":[{"participant_key":"finance","agent_id":"a1","is_entry":true,"initial_responsibility":"..."}],"initial_work_items":[{"work_item_key":"analyze","participant_key":"finance","goal":"...","acceptance_criteria":"..."}],"suggested_edges":[{"source_participant_key":"finance","target_participant_key":"orders","label":"核对退款"}],"content_hash":"..."}
```

- `PATCH /group-formations/{id}` body 为上述可编辑字段加 `expected_revision_no`；If-Match。服务复制成新 revision，200 返回 revision/version；participant/work_item/edge/source 均关系化校验。409 返回 current revision。
- `POST /group-formations/{id}/confirm` body `{expected_revision_no}`；200 返回 `{proposal_status:"confirmed",group_session,group_run,run,nodes[],frozen_revision_no}`。
- `POST /group-formations/{id}/reject|cancel`；终态重复同 key 返回原状态，并关闭 forming GroupSession，不创建节点或 GroupRun。
- `GET /groups/{id}`、`GET /groups/{id}/nodes`、`GET /groups/{id}/suggested-edges`、`GET /groups/{id}/collaboration-edges`、`GET /groups/{id}/timeline`、`GET /groups/{id}/artifacts`。
- `POST /groups/{id}/runs` body `{content,recipient_node_id?,reference_run_ids[]}`，`recipient_node_id` 同时是本轮 owner；省略时取入口节点。202 原子创建新 GroupRun/Run，并返回所有固定节点的 `agent_versions:[{group_node_id,participation_status,agent_version_id?,unavailable_reason?}]`。非 owner 不可用不阻塞创建；owner 不可用返回 409 `group_owner_unavailable`，错误 details 给出调用者有权查看的 `available_node_ids`，且不留下 Run/GroupRun。固定节点不可替换或删除。
- `POST /group-runs/{id}/messages` body `{content,recipient_node_id?}`；空 recipient 明确路由 entry。`POST /group-runs/{id}/cancel`；`POST /groups/{id}/close`。

`GET /groups/{id}/nodes` 返回节点当前 `availability/unavailable_reason/version`，但每个 GroupRun 的执行资格始终以其冻结 `agent_versions` 为准。向本轮 unavailable 节点发送 Message 或 `invoke_agent` 返回 409 `group_node_unavailable`，不创建 WorkItem/Invocation；Agent 重新启用后必须创建下一 GroupRun 才可恢复参与。上述 POST 均要求 Idempotency-Key；同 key 同 hash 重放原响应，不同 hash 返回 409。

GroupRun 响应为 `{id,group_session_id,owner_node_id,display_phase,run:{lifecycle,wait_reason,outcome,version},agent_versions[]}`，不含另一份 status/outcome。`display_phase=created` 仅在 Run running 且尚无 Runner started 时派生；Run waiting 时 display_phase 取 `waiting_user|waiting_agent|waiting_tool`（resource 等内部等待对用户显示 waiting），其他情况等于 Run lifecycle。它不可写、不是事实源。返回 Chat 是前端路由动作，无 API 状态命令。

## 8. Required Actions

RequiredAction 是对 pending Clarification、ToolConfirmation、PlanExecution/GroupFormationProposal confirmation 的只读 union projection，不是另一张状态表；`id` 是底层资源 ID，type 决定动作 URL。

`GET /sessions/{id}/required-actions?status=pending&type=&cursor=` 返回统一结构：

```json
{"items":[{"id":"...","type":"clarification|tool_confirmation|plan_confirmation|group_confirmation","status":"pending","session_id":"s1","run_id":"r1","work_item_id":"w1","created_at":"...","version":2,"expires_at":null,"presentation":{"title":"...","summary":"..."},"action":{"method":"POST","href":"...","required_fields":["answer"]}}],"next_cursor":null}
```

presentation 必须经过可见性/脱敏策略。动作接口：

- `POST /clarifications/{id}/answer` body `{answer}`；仅 target user。
- `POST /tool-confirmations/{id}/approve|reject` body `{arguments_hash,reason?}`；仅 Invocation 原 userId。
- Plan/Group confirm 使用各自资源接口。

均要求 If-Match、Idempotency-Key；相同命令返回原结果，不同决策 409 `action_already_resolved`。

## 9. Memory

Memory API 面向事实版本；内部 `MemoryHead` 只通过 `head_id/head_version` 暴露并发令牌，不提供跨用户 Head 集合。

- `GET /memories?scope=user|user_agent&agent_id=&status=active&limit=&cursor=`：只返回当前 userId；稳定排序 `(updated_at DESC,id)`。
- `GET /memories/{id}` 返回 `{id,head_id,scope,agent_id,fact_key,content,status,version,head_version,sources[],created_at,updated_at}`。sources 仅列用户有权访问的 message/session 引用和安全摘录。
- `GET /memories/{id}/versions?cursor=` 返回该 Head 的历史版本；deleted/superseded 只对资源所有者可见。
- `PATCH /memories/{id}` body `{content}`，要求 If-Match=head_version 与 Idempotency-Key；fact_key/scope/agent_id 不可原地改，锁 Head 后创建 user_edit 新版本，200 返回新 active。并发冲突 409。
- `DELETE /memories/{id}` 要求 If-Match/Idempotency-Key；把 active 置 deleted，204。提交后立即不再召回。

首批不允许 API 创建任意 Memory，也不开放 agent scope。Builder/Admin 普通接口与 Agent owner 均不能读取其他用户 Memory；全局 admin memory 路径不存在。

## 10. Artifact、Notification 与 Trace

- Notification 查询资源对应 `NotificationDelivery`；客户端不能创建或把它当作 Run 状态。
- `GET /sessions/{id}/artifacts?status=&cursor=`、`GET /artifacts/{id}`、`GET /artifacts/{id}/download`（Range/ETag checksum）、`POST /artifacts/{id}/delete`。
- `GET /sessions/{id}/notification-deliveries?status=&event_type=&cursor=`、`GET /notification-deliveries/{id}`：Session 所有者只见安全状态、目标 channel label、attempt_count、last_error_code、delivered_at；不返回 credential/provider 原文。Web/API Session 正常返回空集合。
- Admin `GET /admin/notification-deliveries?status=&channel_account_id=&from=&to=&cursor=` 只返回脱敏运维字段；不能借此读取用户 Message。Notification 不提供手工 create 或复活终态 Delivery；瞬时失败只由同一 Delivery 的自动 retry_wait 状态机重投。
- `GET /runs/{id}/timeline` 使用 session_user 投影；`GET /runners/{id}/trace` 仅用户自己的安全摘要或 admin 脱敏运维视图，Trace 不用于恢复。

## 11. Platform Audit API

- `GET /admin/audit-events?category=&action=&resource_type=&resource_id=&actor_id=&outcome=&from=&to=&cursor=`：admin only，返回 `{id,occurred_at,category,action,actor_safe,resource,outcome,severity,summary,changes_redacted}`。
- `GET /accounts/me/audit-events?category=identity|security&cursor=`：普通用户只见自己的登录、身份和安全设置事件。
- `GET /builder/audit-events?resource_type=agent|skill&resource_id=&cursor=`：builder 只见自己拥有资源的允许子集。

审计接口只读，不使用 Session sequence/SSE；无权资源返回 404/403 依通用规则。

## 12. ExecutionEvent、SSE 与实时 Token

- `GET /sessions/{id}/events?after_sequence=123&scan_limit=500&limit=100`
- `GET /sessions/{id}/events/stream?after_sequence=123`
- `GET /runs/{id}/events...` 仍以父 Session sequence 为 cursor，并应用同一 principal 投影。

REST 响应为 `{items,scanned_through_sequence,session_high_watermark,has_more}`。items 可为空但 scanned cursor 前进。SSE 可靠帧：

```text
id: 124
event: work_item.waiting_user
data: {"event_id":"...","session_id":"...","sequence":124,"summary":"...","payload":{}}
```

过滤区间只有不可见事件时：

```text
id: 130
event: cursor
data: {"scanned_through_sequence":130}
```

客户端使用 `Last-Event-ID`，query 仅作兼容；冲突取较大值。cursor 帧只推进游标，不生成 Timeline 条目。客户端不得要求 sequence 连续。网络至少一次，按 event_id 去重；游标早于保留窗口返回 410 与 earliest_sequence。

Redis live bridge 的 `message.delta` 无可靠 SSE id，不持久化、不重放；`stream.degraded` 表示只丢实时体验。断线/API重启后显示后台继续，最终以 MySQL `message.created` 可靠事件替换临时气泡。

## 13. 权限摘要

user 只访问自己的 Session/Run/Plan/Group/Memory/Artifact/Notification；builder 的蓝图所有权不授予用户内容；admin 管理 Registry 和平台审计但普通接口不开放用户全文。Group Agent 的查询受统一事件投影和 recipient 约束。Agent API Key 只访问绑定入口 Agent 下传入 userId 的固定动作集，每个 AgentInvocation 继续代表原 userId。
