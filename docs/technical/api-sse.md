# API 与 SSE 契约

本文解释外部 HTTP 资源、认证、幂等、错误、分页和事件续读语义；[OpenAPI 3.1](openapi-v1.yaml)是路径、方法、Header、请求/响应 Schema 和枚举的机器可读权威。领域状态以[状态机](state-machines.md)为准，表约束以[数据库契约](database-schema.md)为准。四份契约必须在同一提交中更新，CI 发现差异即失败，不允许实现自行挑选其一。

基路径统一 `/api/v1`，JSON 使用 `snake_case`，时间使用 RFC3339 UTC。除健康检查外，所有资源访问先按部署声明的唯一 `identity_profile` 生成 AuthContext，再校验资源归属。

## 1. 认证与请求主体

### 身份 Profile

| 部署 Profile | 环境 | 认证来源 | userId Header |
| --- | --- | --- | --- |
| `raw_user_id_v1` | dev / 受控 UAT | 本地/UAT `web_session`；或 Agent API Key + caller assertion | 仅 Agent API Key 请求必填 |
| `enterprise_oidc_v1` | 企业生产 | 用户 OIDC Token、workload + OBO 用户 Token，或受限 service principal | 禁止 |

详细信任和映射规则见[身份与 Agent API](../modules/identity-api.md)。Web 登录会话同样必须解析为内部 `account_id`。

### Agent API

第一阶段 Agent API 使用绑定入口 Agent 的 Key，并由调用方直接断言 canonical `userId`：

```http
Authorization: Bearer <agent-api-key>
X-MyClaw-User-Id: <canonical-user-id>
```

平台验证 API Key、绑定 Agent、状态和有效期，但不验证终端用户 Token。`X-MyClaw-User-Id` 是调用方提供的裸身份参数；有效 Key 可以断言部署内任意合法 userId。这是第一阶段受控 UAT/内网假设，不是企业生产身份安全声明。

Header 缺失、为空或不合法返回 400；body/query 不再接受第二份 userId，避免冲突来源。API Key 不得交给浏览器或第三方。网关、反向代理、应用日志和 Trace 必须脱敏 Authorization，并按策略脱敏 userId。

OpenAPI 把生产 `enterpriseBearer` 与 UAT 的 `uatAgentApiKey + rawUserIdHeader` 定义为不同 security requirement；后者两个 scheme 是 AND，不能只持 Key 而省略 Header。Profile 不允许的 scheme 必须在认证层拒绝，不能先按另一种 Bearer 语义解析再 fallback。

每个请求生成不可变 AuthContext：`tenant_id, account_id, account_type, canonical_user_id, authentication_source, identity_profile, external_identity_id?, api_key_id?, asserted_user_id?, entry_agent_id?, client_id?, roles, scopes, credential_revision, account_authorization_revision, request_id`。其中 `account_id` 必填；credential_revision 表示认证来源/客户端版本，account_authorization_revision 表示角色/账号授权输入版本，两者不能混用。`asserted_user_id` 只在 raw Agent API Key 请求存在。生产浏览器虽可使用 OIDC callback 创建的 HttpOnly Cookie，`authentication_source` 仍为 `oidc_user`；`web_session` 仅用于 dev/UAT 本地登录。领域服务只使用 `tenant_id + account_id`，不再从业务 JSON、Header、Agent 输出或 Tool 参数复制主体。

生产 Token 必须验证签名、issuer、audience、subject、expiry/not-before、算法和所需 scope；OBO 还必须验证 workload client 与 delegated user。`X-MyClaw-User-Id` 在生产 Profile 出现时返回 400 `invalid_identity_profile`，不能忽略；`invalid_user_id` 只用于允许 raw Header 的 Profile 中格式不合法或重复来源冲突。

生产浏览器登录使用 Authorization Code + PKCE(S256) 和一次性 state/nonce；callback 服务端换取 Token 后只签发 opaque `Secure; HttpOnly; SameSite=Lax` Session Cookie，不向前端脚本暴露 Provider Token。Cookie 认证的所有状态命令还必须带 Session 绑定的 `X-CSRF-Token` 并通过同源 Origin/Referer 校验；Bearer/OBO/service principal 请求不使用该 Header。`POST /auth/logout` 撤销当前 BrowserAuthSession 并清除 Cookie。完整 TTL、轮换和持久化合同见[身份模块](../modules/identity-api.md)与[数据库契约](database-schema.md)。

## 2. 通用协议

- 状态命令要求 `Idempotency-Key`；可编辑资源另要求 `If-Match: "<version>"`；
- 幂等作用域为 `tenant_id + account_id + authenticated client/api_key + method + canonical path`；保存规范化 request hash；同 key 不同 hash 返回 409；
- 异步 Run 创建返回 202；普通资源创建 201；命令 200/202；逻辑删除可 204；
- 列表使用 `limit<=100&cursor=`，响应 `{items,next_cursor}`；cursor 是绑定 principal、过滤条件和稳定排序键的签名不透明值；
- 所有可编辑资源返回 version；CAS 冲突返回 current version/state；
- 无权读取用户内容返回 404 防枚举；已知管理动作无权执行返回 403；
- Redis 故障不影响创建 Run、读取资源和从 MySQL 续读可靠事件。

错误格式：

```json
{
  "error": {
    "code": "version_conflict",
    "message": "Resource changed",
    "request_id": "uuid",
    "retryable": false,
    "details": {"current_version": 4, "current_state": "waiting_user"}
  }
}
```

常用状态：400 validation、401 authentication、403 authorization、404 not_found、409 state/version/idempotency conflict、410 cursor_expired、422 domain_validation、429 budget/rate limit、503 dependency_unavailable。

## 3. 身份与客户端管理

- `GET /auth/me`：返回当前 Web account 和 canonical userId 安全投影；
- `GET /auth/enterprise/start|callback`：Web 企业联登流程；
- `GET/PATCH /accounts/me`：编辑展示资料；canonical userId 不作为普通字段修改；
- `POST /identity/preprovision`：仅 `raw_user_id_v1` 可用；Agent API Key + `X-MyClaw-User-Id` 幂等创建/取得普通 user 账号；
- Admin `GET/POST /admin/accounts`、`PATCH /admin/accounts/{id}/roles|status`；user account 创建时只有基础 user 角色，service account 没有人类角色并只按 workload scopes 授权；roles 接口对 service account 的非空角色请求返回 422 `domain_validation`；
- Admin `GET/POST /admin/accounts/{id}/external-identities`、`DELETE /admin/accounts/{id}/external-identities/{external_identity_id}`；
- `GET/POST /agents/{agent_id}/api-keys`、`POST .../{key_id}/rotate`、`DELETE .../{key_id}`。

预创建不能传入角色、Owner、tenant 或 Scope；新账号只能获得 user。身份合并、拆分和历史迁移不属于第一阶段普通 API。

生产 ExternalIdentity 管理只接受预配置 issuer 与非空 subject，不接受 email/display name 替代。未知 OIDC subject 的普通请求返回 `identity_mapping_required`；默认不开启 JIT provisioning。

API Key 创建响应只在 201 时返回一次明文 secret；后续只显示 prefix、绑定 Agent、状态和 last_used_at。

## 4. Agent、版本与 Registry

- `GET /agents?status=&visibility=&owner=&cursor=`、`POST /agents`；
- `GET /agents/{id}`、`GET/PATCH /agents/{id}/draft`；
- `POST /agents/{id}/publish` body `{"expected_draft_revision":7}`；
- `POST /agents/{id}/deactivate|reactivate`；
- `GET /agents/{id}/versions`、`GET /agents/{id}/versions/{version_id}`；
- `POST /agents/{id}/draft/test-runs`。

Draft PATCH 可以选择逻辑 `skill_ids`；响应同时返回当前解析预览。Publish 响应必须返回不可变依赖：

```json
{
  "published_version": {
    "id": "av1",
    "version_no": 3,
    "content_hash": "...",
    "model_version_id": "mv4",
    "context_policy_version_id": "pv1",
    "history_policy_version_id": "pv2",
    "memory_write_policy_version_id": "pv3",
    "memory_recall_policy_version_id": "pv4",
    "sandbox_profile_version_id": "spv2",
    "tools": [
      {"tool_id": "t1", "registration_hash": "..."}
    ],
    "skills": [
      {"skill_id": "s1", "skill_version_id": "sv7", "checksum": "..."}
    ]
  }
}
```

Runner/API 不提供“对已发布版本刷新依赖”的命令；采用新的 ModelVersion、PolicyVersion、SandboxProfileVersion 或 SkillVersion 必须发布新 AgentVersion。Tool 的启停、Confirmation、风险和连接配置仍按实时治理规则执行，但 Tool registration hash 不兼容时必须拒绝启动，不能静默漂移。

Registry 路径固定为：

- `GET/POST /admin/models`、`GET /admin/models/{model_id}`、`GET/POST /admin/models/{model_id}/versions`、`POST /admin/models/{model_id}/versions/{model_version_id}/conformance`、`POST /admin/models/{model_id}/versions/{model_version_id}/activate`；
- `GET /admin/tools`、`GET/PATCH /admin/tools/{tool_id}`、`POST /admin/tools/{tool_id}/conformance`；Tool 实现/Schema 只由代码 reconcile，PATCH 只改治理字段；
- `GET/POST /skills`、`GET /skills/{skill_id}`、`GET/POST /skills/{skill_id}/versions`、`POST /skills/{skill_id}/versions/{skill_version_id}/publish`、Admin `POST /admin/skills/{skill_id}/versions/{skill_version_id}/assessment-decision`；
- `GET /admin/policies`、`GET /admin/sandbox-profiles`；Policy/Sandbox 版本由代码或签名 GitOps reconcile，对外管理 API 只读；
- 普通 user/Builder 只经 `/agent-options/{option_type}` 取得当前有权选择的安全投影。

Registry 变更写 PlatformAuditEvent，不进入用户 Session SSE。创建新 immutable version 与把 logical current pointer 指向该版本是两个动作；Model 必须先通过当前 ConformanceRun，Skill 必须满足当前环境的 assessment Gate。Secret 只传 `credential_ref/platform_config_ref`，API 永不返回引用对应的值。

## 5. Session、Message 与 Run

- `GET/POST /sessions`；POST body 只有 `{entry_agent_id}`，账号与 origin 都从可信入口派生：Web=`web`、普通 API/OIDC=`api`、已验签 Channel adapter=`channel`；外部 body 不能伪造 origin；
- `GET/DELETE /sessions/{id}`；
- `GET /sessions/{id}/messages?visibility=&cursor=`；
- `POST /sessions/{id}/messages` body `{content,recipient_agent_id?}`；
- `GET /runs/{id}`、`GET /runs/{id}/work-items`；
- `GET /work-items/{id}`、`GET /runners/{id}`；
- `POST /runs/{id}/cancel` body `{reason}`。

Message POST 同事务创建 user Message、Run、根 WorkItem、queued Runner、ExecutionEvent 和 Outbox，返回 202：

Run mode 由资源上下文派生：普通 Session 在 `permission_mode=direct` 时为 chat，在 `plan_readonly` 时为 plan；Group 消息只通过 GroupRun 资源创建 mode=group。客户端不能在同一消息 body 中任意覆盖 mode 或 permission。

```json
{
  "message": {"id": "m1"},
  "run": {
    "id": "r1",
    "mode": "chat",
    "status": "active",
    "wait_reason": null,
    "terminal_reason": null,
    "version": 1
  },
  "required_actions": [],
  "links": {"events": "/api/v1/sessions/s1/events/stream"}
}
```

Run 是生命周期唯一权威。Plan/Group 响应引用或嵌入同一个 Run，不返回独立 status/outcome。客户端不得通过写 Run status 完成任务。

## 6. Plan Permission Mode 与 WorkingPlan

- `POST /sessions/{id}/plan-mode`：进入 plan_readonly 并创建 Plan Run；body `{goal}`；
- `POST /sessions/{id}/plan-mode/exit`：退出但不批准副作用；
- `GET /runs/{id}/working-plans`；
- `GET /working-plans/{id}`、`GET /working-plans/{id}/updates`；
- `POST /working-plans/{id}/approve-implementation` body `{expected_revision,subject_hash}`；
- `POST /working-plans/{id}/continue-planning` body `{instruction,expected_revision}`。

WorkingPlan 由 Agent 通过 Runtime Tool 更新，用户 API 不编辑冻结 DAG。批准绑定当前 plan revision 和 permission revision；并发更新后旧批准返回 409 `approval_subject_changed`。批准把同一 Session/Run 的后续 Runner permission mode 切回 direct，不创建第二个执行图。

## 7. Agent Network

- `GET /agent-network/cards?query=&domain_tag=&group_enabled=&cursor=`；
- `GET /agent-network/agents/{agent_id}`；
- `POST /runs/{id}/agent-invocations` 通常由 Runtime Coordination Tool 内部使用，外部调试仅 admin/测试配置开放。

搜索只返回当前主体可见、已发布且当前有效的 AgentCard；响应包含 `agent_id, agent_version_id, card_content_hash, declaration, attestation, availability`，不返回 Draft、Instructions、Prompt、Credential 或私有 Memory。

创建 AgentInvocation 时重新解析并固定版本。Group 内目标必须使用本轮 member snapshot；授权拒绝返回/记录 terminal rejected Invocation，不创建目标 Runner。

## 8. Group

- `POST /sessions/{id}/groups` body `{member_agent_ids[],goal,reporter_agent_id?,shared_context_refs[]}`；
- `GET /groups/{id}`、`GET /groups/{id}/memberships`、`GET /groups/{id}/messages`；
- `POST /groups/{id}/runs` body `{content,recipient_agent_id?,reporter_agent_id?,reference_run_ids[]}`；
- `GET /group-runs/{id}`、`GET /group-runs/{id}/members`、`GET /group-runs/{id}/interaction-graph`；
- `POST /group-runs/{id}/messages` body `{content,recipient_agent_id?}`；
- `POST /group-runs/{id}/reporter` body `{reporter_agent_id}`，仅在 waiting_user 的 reporter replacement action 中允许；
- `POST /group-runs/{id}/cancel`；
- `POST /groups/{id}/close`。

创建 Group/GroupRun 重新校验 membership、主体访问、环境兼容、Agent 启停和 published version，并在同一事务固定全部 member snapshot。Reporter 必须是 available member；省略时取入口/发起 Agent。

GroupRun 响应：

```json
{
  "id": "gr1",
  "group_session_id": "g1",
  "reporter_agent_id": "a_product",
  "summary_epoch": 2,
  "delivered_summary_epoch": 1,
  "run": {
    "id": "r1",
    "status": "active",
    "wait_reason": null,
    "terminal_reason": null,
    "version": 9
  },
  "members": [
    {"agent_id": "a_product", "agent_version_id": "av3", "participation_status": "available"}
  ]
}
```

GroupRun 不含自己的 `status/outcome`；display phase 可由 UI 从 Run/Runner 派生但不可写。自然空闲后，Coordinator 创建唯一 summary epoch 并唤醒 Reporter；没有 `finalize_group` API。Reporter 失败需要用户选择替代成员时，通过 RequiredAction 暴露。

## 9. SharedTask、Clarification 与 Approval

- `GET/POST /groups/{id}/shared-tasks`、`PATCH /shared-tasks/{id}`；`depends_on_task_ids` 必须属于同 Group 且无环，只作提示/UI；`result_refs` 使用带 `ref_type` 的 Artifact/Message/Verification 引用，不接受无类型裸 UUID；
- `GET /sessions/{id}/required-actions?status=pending&type=&cursor=`；
- `POST /clarifications/{id}/answer` body `{answer}`；
- `POST /approvals/{id}/approve|reject` body `{subject_hash,reason?}`。

RequiredAction 是 Clarification/Approval 的只读 union projection，不是第二张权威状态表：

```json
{
  "items": [{
    "id": "ap1",
    "type": "plan_implementation|tool|membership|session_grant|reporter_selection|clarification",
    "status": "pending",
    "run_id": "r1",
    "work_item_id": "w1",
    "version": 2,
    "subject_hash": "...",
    "presentation": {"title": "...", "summary": "..."},
    "action": {"method": "POST", "href": "..."}
  }],
  "next_cursor": null
}
```

只有 AuthContext 中 `account_id` 与动作 `target_account_id` 相同的请求可决定。raw Profile 下该 account 由调用方 Header 解析，因此 Key 持有者仍能通过改 Header 冒充目标账号；这是 UAT 已接受限制。生产 OIDC/OBO 不读取该 Header。相同 Idempotency-Key 与相同决定返回原结果，不同决定返回 409 `action_already_resolved`。

## 10. Workspace 与 Artifact

- `GET /sessions/{id}/workspace?root=private|shared&path=`；
- `GET /sessions/{id}/artifacts?visibility=&cursor=`；
- `GET /artifacts/{id}`；
- `GET /artifacts/{id}/download`，支持 Range 和 checksum ETag；
- `POST /artifacts/{id}/delete` 只表示受控 Retention/用户删除意图，不覆盖 body。Legal Hold 返回 409 `legal_hold_active`；接受后由 Retention Service 把对象进入 `unavailable(deletion_pending)`，恢复窗口到期才进入 `deleted`。
- `POST /artifacts/{id}/cancel-deletion` 仅允许 owner 在默认 7 天恢复窗口内对 `unavailable(deletion_pending)` 使用 If-Match 撤销删除；本体和原 checksum 验证通过后恢复 published。强制 Retention、已物理删除、publish_failed 或 storage/checksum 安全恢复不走此接口。

普通 HTTP API 不接受服务器绝对路径。Workspace 访问由 ExecutionPrincipal 解析到当前 Agent 私有根或显式 Group shared 根。Artifact 响应返回 immutable storage identity 的安全引用、checksum、size、visibility 和 provenance；永不返回宿主路径。

`publish_artifact` 是 Agent Tool，不提供“把现有 artifact_id 覆盖成新文件”的 API。更新必须发布新 ID 并带 parent_artifact_id。

## 11. Verification、Timeline 与 Trace

- `GET /runs/{id}/verification-results`；
- `GET /runs/{id}/timeline`；
- `GET /runners/{id}/trace`。

VerificationResult 只引用真实 ToolAttempt、Artifact、文件 checksum 或业务系统事实。普通用户看自己的安全 Trace 摘要；admin 默认也只看脱敏运维投影。Trace 不作为恢复或状态权威。

## 12. Memory

- `GET /memories?scope=&agent_id=&cursor=`：只列当前 account 可访问的 active/tombstone 安全投影；
- `GET/PATCH/DELETE /memories/{memory_id}`：查看、以新 MemoryVersion 修改、或写删除 tombstone；PATCH/DELETE 要求 If-Match 和 Idempotency-Key。

普通 API 不允许把 `account_id`、source Session/Message、policy version 或 agent scope owner 写入 body；这些从既有 Memory 与 AuthContext 派生。修改创建新 MemoryVersion，不覆盖历史；删除后立即停止召回，历史 Runner 的 Capability/Context snapshot 不变化。`scope=agent` 在 v1 不向普通用户开放写入。

## 13. ExecutionEvent REST 与 SSE

- `GET /sessions/{id}/events?after_sequence=123&scan_limit=500&limit=100`；
- `GET /sessions/{id}/events/stream?after_sequence=123`；
- `GET /runs/{id}/events?...` 仍使用父 Session sequence cursor，并应用同一可见性投影。

REST：

```json
{
  "items": [],
  "scanned_through_sequence": 130,
  "session_high_watermark": 145,
  "has_more": true
}
```

items 可以因权限过滤为空，但 scanned cursor 必须前进。SSE 可靠帧：

```text
id: 124
event: work_item.waiting_user
data: {"event_id":"...","session_id":"...","run_id":"...","sequence":124,"event_type":"work_item.waiting_user","visibility":"user","aggregate_type":"work_item","aggregate_id":"...","aggregate_version":4,"summary":"Waiting for your answer","payload":{"schema_version":1,"resource_href":"/api/v1/work-items/...","to_status":"waiting_user"},"occurred_at":"2026-08-06T08:00:00Z"}
```

过滤区间没有可见 Event 时发送 cursor 帧：

```text
id: 130
event: cursor
data: {"scanned_through_sequence":130}
```

客户端优先使用 `Last-Event-ID`；query 参数兼容时取更大值。网络至少一次，按 event_id 去重；sequence 不要求连续。cursor 早于保留窗口返回 410，并给 `earliest_sequence` 和重新同步链接。

## 14. 实时 Token

Redis bridge 只承载非权威 `message.delta`：

- delta 没有可靠 sequence，不保证断线重放；
- `stream.degraded` 只表示实时体验降级，不影响后台执行；
- Provider final 提交为 MySQL Message + ExecutionEvent 后，客户端用可靠消息替换临时气泡；
- Redis/API/浏览器重启后，从最后可靠 sequence 续读；
- 不允许用 Redis 中“最后 token”推断 Run 完成。

## 15. 必须稳定的错误码

| code | 含义 |
| --- | --- |
| `authentication_required` | 缺少 Web 会话、UAT Agent API Key 或生产 Bearer Token |
| `invalid_token` | OIDC/OBO Token 的签名、issuer、audience、subject、时效、算法或 scope 不满足当前 Profile |
| `identity_mapping_required` | 已验证外部身份尚未映射到允许使用的 PlatformAccount |
| `invalid_identity_profile` | 请求认证方式、Header 或部署 Profile 组合不允许 |
| `user_id_required` | Agent API 缺少 `X-MyClaw-User-Id` |
| `invalid_user_id` | raw userId 格式不合法或与请求中重复来源冲突 |
| `forbidden` / `not_found` | 无动作权限 / 内容防枚举 |
| `version_conflict` | If-Match/CAS 失败 |
| `idempotency_conflict` | 同 key 不同请求 hash |
| `immutable_resource` | 修改 published version 的内容/依赖，或覆盖已发布 Artifact；不包含状态机允许的受控准入状态迁移 |
| `action_already_resolved` | 人类动作已终态 |
| `approval_subject_changed` | revision/hash 已变化 |
| `skill_assessment_required` | SkillVersion 缺少当前 assessment，或 unknown 尚未批准/已拒绝/隔离 |
| `agent_unavailable` | 新解析时目标停用或无发布版本 |
| `group_member_unavailable` | 本轮快照目标不可执行 |
| `reporter_unavailable` | 最终 Reporter 需要用户替换 |
| `permission_mode_denied` | plan_readonly 等策略阻止动作 |
| `tool_registration_changed` | AgentVersion 固定的 Tool registration hash 与当前实现不兼容 |
| `sandbox_profile_incompatible` | Agent/Group/Session 固定的 SandboxProfileVersion 无法在当前环境安全运行 |
| `artifact_unavailable` | Artifact 未完成发布、存储/校验失败或已进入删除流程 |
| `legal_hold_active` | Legal Hold 阻止为 Artifact 或其他受管记录创建删除意图 |
| `budget_exhausted` | 硬预算耗尽 |
| `outcome_unknown` | 外部副作用无法确认，禁止盲重试 |
| `event_cursor_expired` | SSE/REST cursor 超出保留窗口 |
| `dependency_unavailable` | 当前 Profile 的 MySQL/Workspace/Artifact/IdP/Provider/Tool 等必需依赖不可用，且未能安全降级 |
| `rate_limited` | 准入、租户、Provider、Tool 或成本限额被命中；details 可给出安全 `retry_after` |
| `domain_validation` | 请求 Schema 合法，但违反当前领域状态或跨资源不变量 |

错误码与 HTTP status 的绑定固定为：401=`authentication_required|invalid_token`；400=`invalid_identity_profile|user_id_required|invalid_user_id`；403=`identity_mapping_required|forbidden|permission_mode_denied`；404=`not_found`；409=`version_conflict|idempotency_conflict|immutable_resource|action_already_resolved|approval_subject_changed|tool_registration_changed|artifact_unavailable|legal_hold_active|outcome_unknown`；410=`event_cursor_expired`；422=`skill_assessment_required|agent_unavailable|group_member_unavailable|reporter_unavailable|sandbox_profile_incompatible|domain_validation`；429=`budget_exhausted|rate_limited`；503=`dependency_unavailable`。OpenAPI `ErrorCode.x-http-status-map` 是同一机器可读对照；实现不得对同一 code 随路由改变 status。

错误 details 只返回调用者有权查看的安全字段，不回显 Prompt、Credential、内部路径或其他用户资源。

## 16. API 契约门禁

[OpenAPI 3.1](openapi-v1.yaml) 已作为目标机器契约纳入文档基线。实现进入 UAT/生产前必须生成或校验服务端路由和客户端类型，并通过：

1. schema/enum 与数据库、状态机一致；
2. 所有状态命令的 Idempotency-Key 与 If-Match 测试；
3. raw Profile 的 userId Header 缺失/冲突、API Key 撤销、同一 Key 多 userId、解析后 account 隔离与审计测试；生产 Profile 另覆盖伪造 issuer/subject、错 audience、过期 Token、OBO client/scope 和 Header 注入；
4. GroupRun 不产生第二份 lifecycle；
5. SkillVersion/AgentVersion 响应可复现；
6. SSE 断线、过滤游标、保留窗口和 Redis 降级测试；
7. Artifact immutable download/checksum 测试；
8. 迟到结果、旧 lease 和重复 summary wake 不改变终态；
9. `/health/ready` 与 release manifest 记录 `deployment_profile + identity_profile + git_sha + image_digest + schema_head + openapi_sha256 + configuration_revision`；不兼容组合必须 not_ready；
10. CI 解析 OpenAPI，验证全部 `$ref`、operationId 唯一、path parameter 完整，并将 Run/WorkItem/Runner/Tool/Invocation/Approval/Group/Artifact 枚举与状态机做双向差集检查；
11. `raw_user_id_v1` 只能与 `dev|uat_single_node` 组合，`enterprise_production_v1` 必须与 `enterprise_oidc_v1` 组合。
