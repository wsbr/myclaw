# 数据库契约

本文是 Agent-native v2 关系模型、主外键、唯一约束和关键索引的技术权威，同时覆盖 `uat_single_node` 与 `enterprise_production_v1`；Profile 特有字段会明确标注。产品语义以[权威模块](../README.md)为准，状态值和迁移以[状态机](state-machines.md)为准。历史 V1 表只能作为迁移输入，不能覆盖本契约。

## 1. 全局约定

- MySQL 8 / InnoDB / `READ COMMITTED`；所有领域写入使用显式事务；
- 应用生成 UUIDv7，公开主键使用 `CHAR(36)`；时间为 UTC `DATETIME(6)`；
- 可变聚合均有 `version BIGINT NOT NULL`，命令使用 CAS；
- 金额、Token、次数和预算使用明确的整数/定点单位，不使用浮点；
- JSON 只保存有界快照或 Provider 原始安全元数据，不保存需要约束、连接或调度的状态；
- 外键默认 `RESTRICT`。执行、审计、版本和 Artifact 不做级联删除；
- 每张 tenant-owned 表物理上必须携带非空 `tenant_id`；所有 tenant-owned 主实体除 `id PK` 外还建立 `UNIQUE(tenant_id,id)`，跨表关系一律用 `(tenant_id,*_id)` 复合 FK，服务查询不得只按裸资源 ID；
- `execution_event`、`platform_audit_event` 和 Attempt 事实只追加；终态事实不可覆盖或复活；
- secret 只保存 hash 或外部 secret reference，不保存明文。

“不可变版本”精确指版本的行为内容、依赖关系、版本号和 `content_hash` 不可改写，不表示安全治理无法停止新的准入。只有本文/状态机明确列出的 `publication_status|status` 迁移可以由 Registry Service 通过 CAS 修改，同事务写 PlatformAuditEvent；状态列、治理 `version` 和状态时间不参与 `content_hash`。任何依赖要采用新内容都必须创建新版本，不能借状态迁移换包。

下文省略所有表共有的 `created_at`、`updated_at`；只追加表没有 `updated_at`。为避免在每个子表重复同一句，字段清单也省略由本节统一要求的 tenant-owned 子表 `tenant_id`、`UNIQUE(tenant_id,id)` 和复合 FK 前缀；这是物理 DDL 必填项，不是可选的逻辑投影。

### Tenant 隔离的物理范围

控制域全局 Registry 白名单只有 `model/model_version`、`platform_tool`、`policy_definition/policy_version`、`sandbox_profile/sandbox_profile_version`、`channel_plugin` 及只针对这些全局对象的 `conformance_run`；它们由部署级平台治理，不能保存用户正文、用户委托 Token 或 Tenant 数据，只能保存指向企业 Secret Manager 的平台 Credential reference。`tenant` 自身是租户根。除此之外，本契约中的 Agent、Skill、身份、Session、Run、Invocation、Group、Memory、Workspace、Artifact、Channel binding/delivery、Event/Audit/Seal 及全部关系/Attempt 表都是 tenant-owned。

tenant-owned join/child 表即使字段清单只写 `run_id FK` 或 `agent_version_id FK`，物理列也必须包含 `tenant_id`，并建立 `(tenant_id,run_id)`、`(tenant_id,agent_version_id)` 等复合 FK；多父表因此必须同时属于同一 Tenant。tenant-owned 表引用白名单全局 Registry 时只需全局 ID FK，反向不得让全局表持有某 Tenant 的正文或 Credential。`outbox_command.tenant_id` 是唯一 nullable 例外：只有签名平台级 maintenance 命令可为空；任何用户/业务 aggregate 的命令必须非空并通过其 `(tenant_id,aggregate_id)` 约束或类型化写服务校验。多态 `aggregate/cause/resource/scope` 字段不能替代这些真实 FK；每个允许类型都要有类型化 nullable FK + CHECK，或在数据库 Trigger 中拒绝跨 Tenant 值。

### 物理类型与迁移规则

- UUID 字段统一 `CHAR(36) CHARACTER SET ascii COLLATE ascii_bin`；hash/checksum 使用 `CHAR(64) ascii_bin`；operation/dedupe key 使用 `VARCHAR(191) ascii_bin`；
- 状态值在应用层使用字符串枚举，数据库使用 `VARCHAR(32)` + `CHECK`，不使用 MySQL `ENUM`，便于 expand/contract；
- `version`、revision、epoch、sequence、attempt、generation 使用 `BIGINT UNSIGNED`，初值分别按本文规定，不允许负数；
- 时间统一 `DATETIME(6)`，由数据库 UTC session 写入；lease/expiry 比较使用数据库时间；
- boolean 使用 `BOOLEAN NOT NULL`；正文使用 `MEDIUMTEXT`；短标签/名称最大 191 字符；reason/error code 最大 191 字符；
- JSON 列使用 MySQL `JSON`，写入前按各对象 Schema 校验，单字段默认上限 256 KiB；Prompt/Message 正文和文件本体不得塞入 JSON；
- 所有 tenant-owned 表至少有以 `tenant_id` 开头的授权/列表索引；ORM repository 方法签名必须先接收 TenantAccountScope，再接收资源 ID；
- Alembic 只允许 expand → backfill → contract。新增非空列先 nullable/backfill/验证再收紧；删除/改名至少跨一个兼容版本；生产禁止自动 downgrade destructive DDL。

### 身份字段唯一解释

以下命名在全部新表、迁移、ORM 和查询中强制统一：

- `asserted_user_id`：只表示入口调用方提交的外部字符串，允许出现在 `AuthContext`、认证日志和审计快照中，不作为领域外键；
- `canonical_user_id`：`platform_account` 内经规范化的外部业务标识，只用于 `(tenant_id, canonical_user_id)` 解析账号；
- `account_id` / `*_account_id`：平台内部稳定 UUID，是 Session、Action、Capability、Artifact 和授权关系唯一允许使用的账号外键；
- 同时保存 `tenant_id + *_account_id` 的表必须建立复合外键，禁止分别校验两个互不关联的单列外键；
- API 边界先在固定 tenant 内把 `asserted_user_id` 或受验证的 OIDC identity 解析为 `platform_account.id`，领域命令从此只携带 `account_id`。Agent、Tool 和下游 AgentInvocation 不得重新解析或改写身份。

因此，`raw_user_id_v1` 只是 UAT 入口认证 Profile，不会让裸字符串成为内部关系键；生产切换到 OIDC/OBO 时，领域表和授权查询不需要更换主外键。

## 2. 身份与客户端

### `tenant`

`id PK`、`key UNIQUE`、`name`、`status active|suspended`、`next_audit_sequence BIGINT NOT NULL DEFAULT 1`、`version`。

第一阶段单租户也必须写真实 `tenant_id`，禁止用 nullable tenant 形成两套查询路径。

### `platform_account`

`id PK`、`tenant_id FK`、`canonical_user_id VARCHAR(191) NOT NULL`、`display_name`、`account_type user|service`、`status active|suspended|closed`、`created_via web|enterprise_login|agent_api|directory_sync|admin`、`authorization_revision BIGINT NOT NULL DEFAULT 1`、`version`；唯一 `(tenant_id,canonical_user_id)` 和 `(tenant_id,id)`。角色、账号状态或其他授权输入变化必须在同一事务递增 authorization_revision；纯展示名修改只递增 version。

第一阶段 `canonical_user_id` 来自企业调用方提供的字符串，不要求 UUID。`tenant_id` 取部署配置中的固定值，不能由 Agent API 请求覆盖；这样保留未来多租户键，但不声称已经验证外部 issuer/subject。

### `external_identity`

`id PK`、`tenant_id FK`、`account_id`、`issuer VARCHAR(512) ascii_bin`、`subject VARCHAR(255) utf8mb4_bin`、`principal_type user|service`、`status active|revoked`、`active_subject_key GENERATED NULL|subject`、`claims_revision`、`bound_by_account_id`、`last_seen_at`、`revoked_at NULL`、`version`；复合 FK `(tenant_id,account_id)` 和 `(tenant_id,bound_by_account_id)` 指向 `platform_account(tenant_id,id)`，唯一 `(tenant_id,issuer,active_subject_key)`。该 nullable active key 保证同一 subject 最多一个 active 绑定，同时允许保留多条 revoked 历史。issuer 保存管理员验证后配置的 OIDC issuer 原值，Token `iss` 必须与它精确匹配，请求期不删尾斜线、换大小写或重写端口；subject 是 IdP 的不透明、大小写敏感字符串，不做业务语义改写。

`external_identity` 是 `enterprise_oidc_v1` 下 `oidc_user|oidc_obo|service_principal` 认证来源的生产身份映射。`issuer + subject` 是受验证的外部身份键；展示名、email 和任意 Header 都不能替代它。UAT 的 `raw_user_id_v1` 不创建伪造的 issuer/subject。

### `tenant_identity_provider`、`workload_client_policy`

- `tenant_identity_provider`：`id PK`、`tenant_id FK`、`issuer`、`jwks_uri`、`audiences_json`、`allowed_algorithms_json`、`required_claims_json`、`clock_skew_seconds`、`jwks_cache_ttl_seconds`、`status active|inactive`、`config_revision`、`content_hash`、`version`；唯一 `(tenant_id,issuer)`；
- `workload_client_policy`：`id PK`、`tenant_id FK`、`identity_provider_id FK`、`client_subject`、`active_client_key GENERATED NULL|client_subject`、`agent_scope all|selected`、`status active|revoked`、`credential_revision`、`content_hash`、`version`；唯一 `(tenant_id,identity_provider_id,active_client_key)`，允许保留 revoked 历史；
- `workload_client_auth_source`：`workload_client_policy_id FK`、`authentication_source oidc_obo|service_principal`；PK 两列；
- `workload_client_scope`：`workload_client_policy_id FK`、`scope`；PK 两列；
- `workload_client_agent`：`workload_client_policy_id FK`、`agent_id FK`；PK 两列，只有 agent_scope=selected 时允许存在且至少一行。

两表由签名部署配置/GitOps reconcile，应用管理 API 不直接修改。`enterprise_oidc_v1` readiness 必须确认至少一个 active IdP、HTTPS issuer/JWKS、非空 audience/algorithm allowlist，以及 OBO/service principal 对应的 active workload policy。配置变化递增 revision 并写 PlatformAuditEvent。

### `oidc_login_transaction`、`browser_auth_session`

- `oidc_login_transaction`：`id PK`、`tenant_id FK`、`identity_provider_id FK`、`state_hash CHAR(64) UNIQUE`、`nonce_hash CHAR(64)`、`pkce_verifier_ciphertext`、`return_to_path`、`status pending|consumed|expired`、`expires_at`、`consumed_at NULL`、`version`。PKCE verifier 使用 KMS envelope encryption；最长 10 分钟，consume 使用 status/version CAS，terminal 后不复用，Maintenance 到期标 expired 并按短期认证数据 Retention 清理；
- `browser_auth_session`：`id PK`、`tenant_id FK`、`account_id`、`external_identity_id FK`、`session_token_hash CHAR(64) UNIQUE`、`csrf_token_hash CHAR(64)`、`oidc_session_id_hash NULL`、`authentication_source oidc_user`、`credential_revision BIGINT NOT NULL DEFAULT 1`、`account_authorization_revision`、`status active|revoked|expired`、`authenticated_at`、`last_seen_at`、`idle_expires_at`、`absolute_expires_at`、`revoked_at NULL`、`version`；复合 FK `(tenant_id,account_id)`。只保存 opaque token/CSRF/IdP session id 的 hash，不保存浏览器可用的 access/refresh token。Session token/CSRF 轮换创建新行或递增 credential_revision 并原子换 hash；lookup 后仍需校验 Account、ExternalIdentity、credential revision 和 account authorization revision；权限/身份撤销时批量 CAS revoke。

登录 callback 必须在一个事务中 CAS consume LoginTransaction、创建新 BrowserAuthSession、撤销同一浏览器的 pre-auth token 并写 PlatformAuditEvent；失败全部回滚。Cookie 值只在响应中返回一次，数据库和日志不保存明文。`enterprise_production_v1` 的空闲/绝对 TTL 固定为 30 分钟/8 小时，更严格的客户策略可以缩短但不能延长。

### `platform_account_role`

`tenant_id FK`、`account_id`、`role user|builder|admin`、`granted_by_account_id`、`granted_at`；PK `(tenant_id,account_id,role)`，复合 FK `(tenant_id,account_id)` 和 `(tenant_id,granted_by_account_id)` 均指向 `platform_account(tenant_id,id)`。

CHECK/Trigger 要求只有 `account_type=user` 可以存在 role 行，且 user 账号始终保留基础 `user` role（closed 账号仅保留历史，授权仍一律拒绝）；builder/admin 是附加角色。`account_type=service` 不得持有 user/builder/admin role，只通过受信 workload policy 与 scopes 授权。角色变更递增 PlatformAccount.authorization_revision，并撤销/轮换现有 BrowserAuthSession。

### `agent_api_key`

`id PK`、`tenant_id FK`、`agent_id FK`、`name`、`caller_label`、`prefix UNIQUE`、`secret_hash`、`status active|revoked|expired`、`credential_revision`、`expires_at`、`last_used_at`、`created_by_account_id`、`version`。

第一阶段 Key 认证调用方并绑定入口 Agent；`X-MyClaw-User-Id` 不写入 Key，而是在每次请求中形成 `asserted_user_id`。有效 Key 可以断言固定 tenant 内任意合法 userId，这是显式接受的 UAT 风险。

### `http_idempotency_record`

`id PK`、`tenant_id FK`、`account_id`、`authentication_source web_session|agent_api_key|oidc_user|oidc_obo|service_principal`、`authenticated_client_type web_app|agent_api_key|oidc_client|service_principal`、`authenticated_client_key VARCHAR(255) ascii_bin`、`method`、`canonical_path`、`idempotency_key`、`request_hash`、`status in_progress|completed|failed`、`response_status NULL`、`response_resource_type NULL`、`response_resource_id NULL`、`response_hash NULL`、`expires_at`、`version`；复合 FK `(tenant_id,account_id)`，唯一 `(tenant_id,account_id,authenticated_client_type,authenticated_client_key,method,canonical_path,idempotency_key)`。`authenticated_client_key` 不是 Secret：UAT Key 使用平台 key id，Web 使用部署应用 client id，OIDC/OBO/service principal 使用受信 issuer+client_id 的稳定 HMAC/内部 id；不使用易变的单次 Cookie session id。

首次命令在领域事务中创建 in_progress 记录并写业务事实；提交响应前更新 completed 和稳定资源引用。同 key/hash 重放从权威资源重建等价响应，不重复执行命令；同 key 不同 hash 返回 409。一次性 Secret 不进入该表；API Key 创建响应丢失时必须 rotate，重放只返回 key metadata 和 `secret_retrievable=false`。

## 3. Registry、Agent 与不可变版本

### `agent`

`id PK`、`tenant_id FK`、`owner_account_id`、`visibility private|enterprise`、`status draft|published|inactive`、`current_published_version_id FK NULL`、`draft_version_id FK`、`next_version_no`、`version`；复合 FK `(tenant_id,owner_account_id) → platform_account(tenant_id,id)`。

### `agent_member`

`id PK`、`tenant_id FK`、`agent_id`、`account_id`、`status active|revoked`、`granted_by_account_id`、`granted_at`、`revoked_at NULL`、`version`、时间戳；唯一 `(agent_id,account_id)`，复合 FK `(agent_id,tenant_id) → agent(id,tenant_id)`、`(account_id,tenant_id)` 和 `(granted_by_account_id,tenant_id) → platform_account(id,tenant_id)`，并索引 `(account_id,status,agent_id)` 与 `(agent_id,status,account_id)`。

该表只表达 private Agent 的可撤销使用白名单，不表达 Owner 或配置协作者。Owner 保存在 `agent.owner_account_id` 且不得重复写入成员表；enterprise Agent 不读取此表。授权只对 active TenantMembership + active PlatformAccount 生效，并在新 Session、消息/Run、AgentInvocation、Group 选择和定时分派前实时重读。撤销保留历史行和平台审计，不删除成员已有 Session。

### `agent_version`

`id PK`、`agent_id FK`、`publication_status draft|published|deprecated|draft_test_snapshot`、`version_no NULL`、`draft_revision NULL`、`draft_agent_key GENERATED NULL|agent_id`、`derived_from_version_id FK NULL`、`name`、`description`、`instructions MEDIUMTEXT`、`model_id FK`、`model_version_id FK NULL`、`context_policy_id FK`、`context_policy_version_id FK NULL`、`history_policy_id FK`、`history_policy_version_id FK NULL`、`memory_write_policy_id FK`、`memory_write_policy_version_id FK NULL`、`memory_recall_policy_id FK`、`memory_recall_policy_version_id FK NULL`、`sandbox_profile_id FK`、`sandbox_profile_version_id FK NULL`、`group_enabled`、`discovery_config_json`、`collaboration_config_json`、`content_hash CHAR(64)`、`published_at`、`published_by_account_id FK`、`version`。

约束：唯一 `(agent_id,version_no)`（published/deprecated）；`draft_agent_key=agent_id` 当且仅当 status=draft，唯一 `(draft_agent_key)` 保证每个 Agent 只有一个 draft；draft 保存逻辑选择并允许 snapshot FK 为空，published/deprecated/draft_test_snapshot 必须同时保存全部精确 version FK；后面三类的行为字段和依赖关系在数据库 Trigger/权限层拒绝 UPDATE/DELETE，仅 published 允许按状态机 CAS 迁移为 deprecated。发布使用 copy-on-publish，不把 draft 行原地改为 published。

### `agent_version_tool`

`agent_version_id FK`、`platform_tool_id FK`、`tool_registration_hash CHAR(64)`、`sort_order`；PK `(agent_version_id,platform_tool_id)`。Tool 平台连接配置、启停、确认策略和风险版本不复制到此表，它们是执行前重读的实时治理事实。

### `agent_version_skill`

`agent_version_id FK`、`skill_version_id FK`、`checksum CHAR(64)`、`sort_order`；PK `(agent_version_id,skill_version_id)`，并唯一 `(agent_version_id,sort_order)`。

发布事务从 Draft 的逻辑 Skill 选择解析精确 SkillVersion 后复制此表。同一 published AgentVersion 不得因 `skill.current_version_id` 变化而改变行为。

### `agent_card`

`id PK`、`tenant_id FK`、`agent_id FK`、`agent_version_id FK UNIQUE`、`visibility private|enterprise`、`name`、`description`、`identity_kind`、`domain_tags_json`、`supported_outcomes_json`、`accepted_request_summary`、`capabilities_summary_json`、`collaboration_summary_json`、`cost_class`、`latency_class`、`availability available|unavailable`、`group_enabled`、`declaration_json`、`attestation_json`、`content_hash CHAR(64)`、`published_at`。只为 published/deprecated 版本创建，不保存 Instructions、Credential、私有 Memory 或 Prompt；`declaration_json` 只含 Builder 自述字段，`attestation_json` 只含平台签发证据引用。

### `model`、`model_version`

- `model`：`id PK`、`key UNIQUE`、`name`、`current_version_id FK NULL`、`status active|inactive`、`next_version_no`、`version`；
- `model_version`：`id PK`、`model_id FK`、`version_no`、`provider`、`model_name`、`endpoint`、`credential_ref`（迁移期物理列；保存版本化密文或内部部署引用，API 不返回）、`default_parameters_json`、`capabilities_json`、`content_hash`、`published_at`、`published_by_account_id FK`，只追加；唯一 `(model_id,version_no)` 和 `(model_id,content_hash)`。

历史 `env://` reference 在 Secret Manager 中轮换时不修改 ModelVersion；由公开 v2 API 管理的加密 API Key 轮换，以及任何 endpoint、model name、参数、能力或 Adapter 协议变化，都必须发布新 ModelVersion。只有存在当前必需 suite 的 passed ConformanceRun 的版本可进入 AgentVersion 快照。

### `conformance_run`

`id PK`、`subject_type model_version|platform_tool|channel_plugin|sandbox_profile_version`、`model_version_id FK NULL`、`platform_tool_id FK NULL`、`channel_plugin_id FK NULL`、`sandbox_profile_version_id FK NULL`、`subject_key GENERATED`、`suite_version`、`idempotency_key`、`request_hash`、`status queued|running|passed|failed`、`started_at NULL`、`ended_at NULL`、`result_summary_json NULL`、`evidence_artifact_id FK NULL`、`content_hash NULL`、`version`。CHECK 要求四个 subject FK 恰好一个非空且与 `subject_type` 一致；`subject_key` 由该非空 UUID 生成。唯一 `(subject_type,subject_key,suite_version,idempotency_key)`，索引 `(subject_type,subject_key,suite_version,status,ended_at)`。OpenAPI 的 `subject_id` 是四个物理 FK 投影出的通用 wire 字段，不对应无外键的数据库列。

queued/running 通过 CAS 推进，terminal 后由 Trigger 拒绝修改/删除。新的测试创建新 ConformanceRun；不覆盖旧结果。Registry/Agent publish 只接受平台配置指定 suite_version 的最新 terminal passed 结果。

### `platform_tool`

`id PK`、`key UNIQUE`、`name`、`description`、`implementation_ref`、`input_schema_json`、`output_schema_json`、`execution_mode inline|durable_async`、`side_effect none|idempotent|non_idempotent`、`supports_cancel`、`supports_status_lookup`、`registration_hash`、`enabled`、`confirmation_policy never|per_call|session_grant`、`platform_config_ref`、`config_revision`、`risk_version`、`version`。

Registry reconcile 以代码注册为来源校验 key/implementation/schema/registration_hash；管理员只能改名称、说明、enabled、confirmation_policy 和 platform_config_ref。AgentVersionTool 固定 registration_hash；若当前 Tool 的 registration_hash 不兼容，新的 Runner 明确失败 `tool_registration_changed`，不得静默使用新 Schema。enabled、confirmation_policy、config/risk revision 是刻意的实时治理事实，执行前重读，不冻结进 AgentVersion。

### `skill`、`skill_version`

- `skill`：`id PK`、`tenant_id FK`、`owner_account_id`、`name`、`description`、`current_version_id FK NULL`、`status active|inactive`、`next_version_no`、`version`；复合 FK `(tenant_id,owner_account_id)`；
- `skill_version`：`id PK`、`skill_id FK`、`version_no`、`package_uri`、`checksum`、`manifest_json`、`package_size`、`archive_type`、`status uploaded|validating|published|rejected|deprecated|quarantined`、`content_hash`、`published_at`、`published_by_account_id FK`；内容只追加，唯一 `(skill_id,version_no)` 和 `(skill_id,checksum)`。数据库 Trigger 禁止内容 UPDATE 和整行 DELETE，只允许固定状态迁移。

AgentVersionSkill 只能引用 published/deprecated SkillVersion；quarantined 阻止新的装配和执行，但不删除历史包与引用。

生产另有：

- `skill_assessment`：`id PK`、`skill_version_id FK`、`assessment_revision`、`source_class`、`scanner_versions_json`、`status trusted|unknown|quarantined`、`findings_summary_json`、`content_hash`、`created_at`，只追加；唯一 `(skill_version_id,assessment_revision)`；
- `skill_assessment_decision`：`id PK`、`skill_assessment_id FK UNIQUE`、`decision approved|rejected`、`decided_by_account_id FK`、`reason`、`created_at`，只追加。

生产执行取最新适用 assessment：trusted 可直接准入，unknown 必须存在 approved decision，quarantined 或 rejected 一律拒绝新发布/执行。重新扫描创建新 assessment revision，旧 decision 不自动覆盖新 revision。

### `policy_definition`、`policy_version`

- `policy_definition`：`id PK`、`key UNIQUE`、`policy_type context|history|memory_write|memory_recall`、`name`、`description`、`current_version_id FK NULL`、`status active|inactive`、`next_version_no`、`version`；
- `policy_version`：`id PK`、`policy_definition_id FK`、`version_no`、`implementation_ref`、`parameters_json`、`content_hash`、`status published|deprecated`、`published_at`、`version`；内容只追加，唯一 `(policy_definition_id,version_no)` 和 `(policy_definition_id,content_hash)`。

AgentVersion 的四个逻辑 Policy FK 必须分别匹配 policy_type，发布时解析并固定四个精确 PolicyVersion。显式 `none` 也是已发布 PolicyVersion，字段不得为空。

### `sandbox_profile`、`sandbox_profile_version`

- `sandbox_profile`：`id PK`、`key UNIQUE`、`name`、`sandbox_type local|remote_isolated`、`current_version_id FK NULL`、`status active|inactive`、`next_version_no`、`version`；
- `sandbox_profile_version`：`id PK`、`sandbox_profile_id FK`、`version_no`、`provider_ref`、`runtime_class`、`image_digest`、`workspace_protocol_version`、`artifact_protocol_version`、`resource_policy_json`、`egress_policy_json`、`mount_policy_json`、`security_level dev_directory|microvm`、`content_hash`、`status published|deprecated`、`published_at`、`version`；内容只追加，唯一 `(sandbox_profile_id,version_no)` 和 `(sandbox_profile_id,content_hash)`。

`enterprise_production_v1` 只允许 `security_level=microvm`。SessionSandbox 固定 `sandbox_profile_version_id`；Profile 后续发布不热迁移已有 Session。GroupRun 成员可以使用不同 Provider，但必须通过相同 Workspace/Artifact protocol version 与当前 Session mount policy 的兼容检查，否则创建时返回 `sandbox_profile_incompatible`。

以上 Registry 版本表的内容列由 Trigger 拒绝 UPDATE，整行拒绝 DELETE；只允许状态机明确的准入状态 CAS 迁移。依赖版本（Skill/Policy/Sandbox）的 `deprecated` 阻止新 AgentVersion 选择/发布，但已固定该精确依赖的 AgentVersion 仍可启动和恢复 Runner；AgentVersion 自身的 `deprecated` 阻止它被新 Run/Invocation 解析，但已创建工作可按原快照恢复。SkillVersion 的 `quarantined` 及逻辑 Registry 对象的 `inactive` 是强准入禁止，阻止尚未开始的新 Run/Runner，但不修改已提交历史事实。这些状态均不改写 AgentVersion 历史关系或任何版本内容。

## 4. Session、消息与唯一 Run

### `user_session`

`id PK`、`tenant_id FK`、`account_id`、`entry_agent_id FK`、`origin web|api|channel|schedule`、`origin_channel_account_id FK NULL`、`origin_external_conversation_id NULL`、`origin_schedule_id FK NULL`、`status active|closed|archived`、`permission_mode direct|plan_readonly`、`permission_revision`、`session_sandbox_id FK NULL`、`next_event_sequence BIGINT NOT NULL DEFAULT 1`、`version`；复合 FK `(tenant_id,account_id) → platform_account(tenant_id,id)`。ChannelConversationBinding 在同一事务随后引用 Session，避免循环外键；schedule 来源必须能由 `origin_schedule_id` 回溯任务定义。

索引：`(tenant_id,account_id,updated_at,id)`、`(entry_agent_id,status)`。

### `agent_schedule`、`agent_schedule_fire`、`runner_schedule_snapshot`

- `agent_schedule`：纯触发器，含 `id PK`、owner tenant/account/user、`definition_tool_invocation_id UNIQUE FK`、`schedule_type once|cron|interval`、`run_at|cron_expression|interval_seconds` 三选一、`timezone`、`next_fire_at UTC`、`end_at NULL`、`max_occurrences NULL`、`occurrence_count`、`overlap_policy skip|queue_one|allow`、`misfire_policy skip|fire_once`、`misfire_grace_seconds`、`status active|paused|completed|disabled`、实际运行计数/最近错误、lease 三元组和 `version`；到期索引 `(status,next_fire_at,id)`。0049 前语义列仅作历史兼容，新记录置空；
- `agent_schedule_fire`：`id PK`、任务/tenant/account FK、`trigger_kind scheduled|manual`、`operation_key`、`scheduled_for UTC`、`status queued|deferred|dispatching|running|succeeded|failed|skipped|cancelled`、Schedule version、`definition_tool_invocation_id`、`definition_hash`、`occurrence_no`、timezone 快照、AgentVersion/Session/Message/Run/Runner FK、终态与未读通知时间、错误和分派 lease；唯一 `(schedule_id,operation_key)`、`run_id`、`runner_id`；
- `runner_schedule_snapshot`：`runner_id PK`、`fire_id UNIQUE`、`schedule_id`、`schedule_version`、`scheduled_for`、`timezone`、定义 ToolInvocation/hash、`agent_version_id`、`account_auth_revision`、`created_at`，只追加。

任务名称、指令、上下文方式、来源 Session、Agent 版本策略和参与人保存在既有 `tool_invocation.authorization_snapshot.agent_schedule_definition_v1`。每次 Fire 以引用和 hash 固定定义；每次触发都创建新的 Run → WorkItem → Runner → AgentTurn，绝不复用历史 WorkItem。

调度输入的 Message 固定 `sender_type=system, role=user`，从而进入当前模型任务但不满足用户记忆的来源条件。Scheduler 只能创建普通 Chat Run 和 Runtime Outbox，不能直接执行模型或 Tool。

### `message`

`id PK`、`session_id FK`、`run_id FK NULL`、`group_session_id FK NULL`、`group_run_id FK NULL`、`sender_kind user|agent|runtime`、`sender_agent_id FK NULL`、`recipient_agent_id FK NULL`、`visibility private|direct|group|user`、`content MEDIUMTEXT`、`content_hash`、`channel_account_id FK NULL`、`provider_message_id NULL`、`sequence`、`created_by_runner_id FK NULL`。

约束：唯一 `(session_id,sequence)` 和 nullable `(channel_account_id,provider_message_id)`；sender/recipient 与 visibility 的组合由 CHECK 和领域校验约束。Runtime 状态摘要必须标记 sender_kind=runtime，不伪装成 Agent 发言。

### `run`

`id PK`、`session_id FK`、`mode chat|plan|group`、`status active|waiting_user|waiting_approval|waiting_tool|waiting_agent|completed|cancelled|failed`、`wait_reason NULL|waiting_user|waiting_approval|waiting_tool|waiting_agent`、`terminal_reason NULL`、`root_work_item_id FK NULL`、`permission_mode_snapshot direct|plan_readonly`、`hard_budget_json`、`deadline_at NULL`、`cancellation_requested_at NULL`、`completed_at NULL`、`version`。CHECK 要求非 waiting 状态的 wait_reason 为空、终态 completed_at 非空、非终态 completed_at 为空。deadline 是持久事实，由 Worker 扫描而不是进程内 timer 驱动。

`run.status` 是所有模式的唯一 lifecycle/terminal 权威；不另存 outcome 镜像。成功、部分完成和业务失败由 `terminal_reason + verified/accepted 投影 + 最终消息` 表达，状态枚举见状态机。

索引：`(session_id,created_at,id)`、`(status,updated_at,id)`。

## 5. WorkingPlan 与人类动作

### `working_plan`、`plan_update`

- `working_plan`：`id PK`、`session_id FK`、`run_id FK`、`owner_agent_id FK`、`summary`、`revision`、`status active|superseded|closed`、`active_owner_key GENERATED NULL|owner_agent_id`、`version`；唯一 `(run_id,active_owner_key)` 保证每个 Agent 在一个 Run 中只有一个 active head；
- `plan_update`：`id PK`、`working_plan_id FK`、`revision`、`items_json`、`explanation`、`updated_by_runner_id FK`、`content_hash`；唯一 `(working_plan_id,revision)`，只追加。

Plan 模式不创建冻结 DAG 或 PlanExecution lifecycle。`permission_mode=plan_readonly` 是 Session/Run 的机器权限事实；用户批准记录在 Approval 中。

### `clarification`、`approval`

- `message_mention`：`id PK`、`message_id FK`、`tenant_id FK`、`target_account_id FK`、`display_name_snapshot`、`handle_snapshot`、`created_at`；唯一 `(message_id,target_account_id)`。账号 ID 由 API 在当前 Agent 可触达成员集合中解析，模型只接收签名 principal reference；
- `clarification`：`id PK`、`tenant_id FK`、`run_id FK`、`work_item_id FK`、`requester_account_id FK`、`target_account_id FK`、兼容期 `target_user_id`、`target_display_name_snapshot`、`target_handle_snapshot`、`question`、`context_summary NULL`、`idempotency_key`、`status open|answered|cancelled`、`answer_message_id FK NULL`、`deadline_at NULL`、`version`；唯一 `(work_item_id,idempotency_key)`；
- `approval`：`id PK`、`tenant_id FK`、`run_id FK`、`work_item_id FK`、`approval_type plan_implementation|tool|membership|session_grant`、`target_account_id`、`subject_ref_type`、`subject_ref_id`、`subject_hash`、`idempotency_key`、`request_hash`、`status pending|approved|rejected|cancelled`、`decision_message_id FK NULL`、`deadline_at NULL`、`version`；复合 FK `(tenant_id,target_account_id) → platform_account(tenant_id,id)`，唯一 `(approval_type,subject_ref_type,subject_ref_id,subject_hash)` 和 `(work_item_id,idempotency_key)`。

相同 idempotency key/request hash 返回既有动作；同 key 不同 hash 冲突。结算使用 `id + expected version + current status` CAS，因此同一个 open/pending 动作只能结算一次。

## 6. WorkItem、Runner 与模型回合

### `work_item`

`id PK`、`run_id FK`、`agent_id FK`、`agent_version_id FK`、`purpose root|delegation|group_message|group_summary`、`parent_work_item_id FK NULL`、`status queued|running|waiting_user|waiting_approval|waiting_tool|waiting_agent|waiting_resource|succeeded|failed|cancelled|timed_out|blocked`、`wait_reason NULL|waiting_user|waiting_approval|waiting_tool|waiting_agent|waiting_resource`、`active_runner_id FK NULL`、`execution_epoch BIGINT`、`resume_cursor_json`、`result_message_id FK NULL`、`version`。CHECK 要求 waiting 状态/原因一一对应，终态没有 active_runner_id。

约束：一个 WorkItem 只有一个 `active_runner_id`；恢复递增 execution_epoch 并创建新 Runner，不复活旧 Runner。

### `runner`

`id PK`、`work_item_id FK`、`execution_epoch`、`status queued|running|succeeded|failed|cancelled|lost`、`lease_owner NULL`、`lease_token NULL`、`lease_expires_at NULL`、`heartbeat_at NULL`、`capability_snapshot_id FK NULL`、`started_at NULL`、`ended_at NULL`、`failure_code NULL`、`version`。CHECK 要求 running 时 lease/started_at 非空，终态 ended_at 非空且 lease 字段清空。

唯一 `(work_item_id,execution_epoch)`；索引 `(status,lease_expires_at)`。所有 Runner 写入必须带 `runner_id + execution_epoch + lease_token` fencing 条件。

### `capability_snapshot`

`id PK`、`runner_id FK UNIQUE`、`tenant_id FK`、`account_id`、`authentication_source web_session|agent_api_key|oidc_user|oidc_obo|service_principal`、`api_key_id FK NULL`、`external_identity_id FK NULL`、`asserted_user_id_snapshot NULL`、`actor_agent_id FK`、`agent_version_id FK`、`permission_revision`、`credential_revision`、`account_authorization_revision`、`assembled_json`、`effective_json`、`denied_json`、`budget_json`、`content_hash`；复合 FK `(tenant_id,account_id) → platform_account(tenant_id,id)`。

Agent API 来源的 `asserted_user_id_snapshot` 是调用方断言，不代表平台验证了终端用户本人。Snapshot 的授权主体始终是 `account_id`，并保留 `api_key_id` 或 `external_identity_id`，便于区分真实 caller、认证方式与 represented account。

### `agent_turn`、`provider_attempt`

- `agent_turn`：`id PK`、`runner_id FK`、`turn_no`、`input_manifest_json`、`provider_stop_reason NULL`、`schema_repair_count`、`usage_json`、`status started|waiting_actions|completed|failed`、`final_message_id FK NULL`；唯一 `(runner_id,turn_no)`；
- `provider_attempt`：`id PK`、`agent_turn_id FK`、`attempt_no`、`provider_request_id NULL`、`request_hash`、`continuation_ref NULL`、`status queued|running|succeeded|failed|timed_out|outcome_unknown`、`received_payload`、`first_payload_at NULL`、`error_code NULL`、`usage_json`、`started_at NULL`、`ended_at NULL`；唯一 `(agent_turn_id,attempt_no)`。

Provider 原始秘密、完整授权头和不允许持久化的 reasoning 不进入数据库。

### `runner_checkpoint`

`id PK`、`work_item_id FK`、`runner_id FK`、`execution_epoch`、`checkpoint_no`、`context_cursor_json`、`working_plan_revision NULL`、`pending_refs_json`、`safe_provider_state_ref NULL`、`content_hash`；唯一 `(runner_id,checkpoint_no)`，只追加。

### `memory_fact`、`memory_version`、`history_summary`

- `memory_fact`：`id PK`、`tenant_id FK`、`account_id NULL`、`agent_id FK NULL`、`scope user|user_agent|agent`、`fact_key`、`current_version_id FK NULL`、`status active|deleted`、`version`；nullable 复合 FK `(tenant_id,account_id)`，CHECK 要求 user=(account 非空/agent 空)、user_agent=(两者非空)、agent=(account 空/agent 非空)；唯一 active fact 通过 `(tenant_id,account discriminator,scope,agent discriminator,fact_key,active_key)` generated key 保证；
- `memory_version`：`id PK`、`memory_fact_id FK`、`version_no`、`content MEDIUMTEXT`、`content_hash`、`source_session_id FK`、`source_message_refs_json`、`created_by_runner_id FK NULL`、`policy_version_id FK`、`created_at`，只追加；唯一 `(memory_fact_id,version_no)`；
- `history_summary`：`id PK`、`session_id FK`、`group_session_id FK NULL`、`agent_id FK`、`through_sequence`、`content MEDIUMTEXT`、`content_hash`、`status active|superseded`、`active_scope_key GENERATED`、`version`；唯一 `(session_id,group discriminator,agent_id,through_sequence)` 和当前 active scope key。

Memory 的修改创建新 MemoryVersion 并 CAS 更新 current pointer；删除只写 tombstone/status，历史来源保持。Recall 只能用 `tenant_id + account_id + scope + agent_id` 索引查询 active head。Group history summary 按 group+agent 隔离，不能用一份全群摘要绕过 visibility。

### `knowledge_base`、文档源、索引与检索快照

- `knowledge_base`：`id PK`、`tenant_id`、`owner_account_id`、`name`、`description`、`sharing_policy owner_only|selected_agents`、`status active|deleted`、`current_index_revision_id NULL`、`pending_index_revision_id NULL`、`embedding_model_id NULL`、`current_lexicon_revision_id NULL`、`next_revision_no`、`next_lexicon_revision_no`、`chunking_profile_json`、`embedding_profile_json`、`version`；唯一 `(tenant_id,owner_account_id,name)` 和 `(id,tenant_id)`；
- `knowledge_base_agent_access`：`id PK`、`tenant_id`、`knowledge_base_id`、`agent_id`、`granted_by_account_id`、`created_at`；唯一 `(knowledge_base_id,agent_id)`，三个父关系均使用 Tenant 复合 FK；
- `knowledge_document`：`id PK`、`tenant_id`、`knowledge_base_id`、`logical_key`、`title`、`status active|deleted`、`current_source_id NULL`、`deleted_at NULL`、`version`；唯一 `(knowledge_base_id,logical_key)`；
- `knowledge_source`：`id PK`、`tenant_id`、`knowledge_base_id`、`document_id`、`checksum`、`filename`、`media_type`、`size_bytes`、`storage_key UNIQUE`、`status indexed|superseded|deleted|failed`、`parse_metadata_json`、`error_code/detail NULL`、`created_by_account_id`、`created_at`；同一 KnowledgeBase 内 checksum 唯一；
- `knowledge_lexicon_revision`：`id PK`、`tenant_id`、`knowledge_base_id`、`revision_no`、`terms_json`（稳定 term id、规范词、类型、说明、规范化别名和 sparse index）、`content_hash`、`created_by_account_id`、`created_at`；同一 KnowledgeBase 内 revision_no 和 content_hash 唯一，只追加；
- `knowledge_index_revision`：`id PK`、`tenant_id`、`knowledge_base_id`、`revision_no`、`status queued|running|ready|failed`、`source_manifest_json`、`source_manifest_hash`、`chunking_profile_json`、`embedding_profile_json`、`embedding_model_version_id NULL`、`lexicon_revision_id NULL`、`retrieval_profile_json`、`vector_backend mysql_json|qdrant`、`vector_collection NULL`、`vector_point_count`、`document_count`、`chunk_count`、`content_hash`、`error_code/detail NULL`、`created_by_account_id`、`built_at`、`started_at/completed_at NULL`；revision_no 和 content_hash 均在 KnowledgeBase 内唯一；
- `knowledge_chunk`：`id PK`、`tenant_id`、`index_revision_id`、`knowledge_base_id`、`document_id`、`source_id`、`ordinal`、`page_number/section NULL`、`content`、`content_hash`、`term_frequencies_json`、`lexicon_term_ids_json`、`vector_json`、`embedding_checksum NULL`、`token_count`；唯一 `(index_revision_id,source_id,ordinal)`；
- `knowledge_retrieval_snapshot`：`id PK`、`tenant_id`、`account_id`、`runner_id`、`agent_id`、`query_hash`、`revision_manifest_json`、`revision_manifest_hash`、`result_snapshot_json`、`retrieval_profile_json`、`degraded_routes_json`、`content_hash`、`created_at`；唯一 `(runner_id,query_hash,revision_manifest_hash)`。

源字节位于受配置约束的 checksum-addressed storage root，不进入 JSON；数据库保存 storage key 和 checksum。MySQL 是正文、授权、revision 和引用的权威，Qdrant 只保存带 `tenant_id + knowledge_base_id + index_revision_id` payload 的可重建向量投影。当前 source/index 指针可推进，但 ready Source、IndexRevision、Chunk 和 RetrievalSnapshot 是历史证据，不原地改写。Agent 检索必须同时匹配原始 `tenant_id + owner_account_id`、显式 Agent grant 和 Runner authority；删除通过新空/缩减 IndexRevision 停止未来检索，历史 Runner 引用仍可解析。

## 7. Tool 与 Agent 调用

### `tool_invocation`、`tool_attempt`

- `tool_invocation`：`id PK`、`run_id FK`、`work_item_id FK`、`agent_turn_id FK`、`tool_id FK`、`tool_key_snapshot`、`arguments_json`、`arguments_hash`、`operation_key`、`idempotency_scope_hash`、`status created|waiting_confirmation|waiting_resource|queued|running|succeeded|failed|rejected|cancelled|outcome_unknown`、`approval_id FK NULL`、`session_grant_id FK NULL`、`active_attempt_id FK NULL`、`execution_epoch`、`deadline_at NULL`、`result_json NULL`、`error_code NULL`、`version`；
- `tool_attempt`：`id PK`、`tool_invocation_id FK`、`attempt_no`、`status queued|running|succeeded|failed|timed_out|cancelled|outcome_unknown`、`lease_owner NULL`、`lease_token NULL`、`lease_expires_at NULL`、`heartbeat_at NULL`、`execution_epoch`、`fencing_token`、`request_hash`、`side_effect_started`、`outcome_confirmed`、`provider_operation_id NULL`、`request_snapshot`、`result_json NULL`、`error_code NULL`、`started_at NULL`、`ended_at NULL`；唯一 `(tool_invocation_id,attempt_no)`，索引 `(status,lease_expires_at)`。

唯一 `(tool_id,idempotency_scope_hash,operation_key)`；相同 operation key 但请求 hash 不同必须冲突。外部副作用 outcome_unknown 禁止盲目重试。

### `session_grant`

`id PK`、`tenant_id FK`、`account_id`、`session_id FK`、`tool_id FK`、`tool_key_snapshot`、`sandbox_profile_version_id FK`、`arguments_constraint_json`、`arguments_constraint_hash`、`risk_version`、`account_authorization_revision`、`approval_id FK UNIQUE`、`status active|revoked|expired`、`expires_at`、`revoked_at NULL`、`version`；复合 FK `(tenant_id,account_id)`，索引 `(tenant_id,account_id,session_id,tool_id,status,expires_at)`。当前账号 authorization_revision 不匹配时 Grant 视为失效并由 Maintenance 标 revoked。

Grant 只由已批准的 `approval_type=session_grant` 创建；参数匹配使用规范化约束与 hash，执行前同时核对 account/session/tool/sandbox/risk/expiry/status。撤销和过期不删除记录，不能扩大既有 Grant 的 arguments constraint；扩大范围必须创建新 Approval 和新 Grant。

### `agent_invocation`

`id PK`、`run_id FK`、`caller_work_item_id FK`、`caller_agent_id FK`、`target_agent_id FK`、`target_agent_version_id FK NULL`、`target_work_item_id FK NULL`、`group_session_id FK NULL`、`group_run_id FK NULL`、`request_message_id FK NULL`、`return_route caller|direct|group`、`status created|queued|running|succeeded|failed|cancelled|timed_out|rejected`、`operation_key`、`request_hash`、`deadline_at NULL`、`result_message_id FK NULL`、`error_code NULL`、`version`。

唯一 `(caller_work_item_id,operation_key)`。授权拒绝也创建 terminal rejected 记录；只有成功校验后才允许 target_work_item 非空。

## 8. Group 聚合

### `group_session`、`group_membership`

- `group_session`：`id PK`、`session_id FK`、`status open|closed|archived`、`initiator_agent_id FK`、`membership_policy fixed_for_run|dynamic_future`、`version`；
- `group_membership`：`id PK`、`group_session_id FK`、`agent_id FK`、`status invited|joined|declined|cancelled|left|removed`、`invited_by_kind user|agent|runtime`、`invited_by_account_id FK NULL`、`invited_by_agent_id FK NULL`、`reason`、`context_scope_json`、`authorization_source`、`joined_at`、`ended_at`、`active_agent_key GENERATED NULL|agent_id`、`version`。CHECK 要求 invited_by_kind 与两个 actor FK 组合一致；唯一 `(group_session_id,active_agent_key)`。

一个 Agent 的 membership 历史只追加事件；`active_agent_key=agent_id` 当且仅当 `ended_at IS NULL AND status IN ('invited','joined')`，其他状态为 NULL，利用 MySQL 允许多个 NULL 的唯一索引保证最多一条当前 membership。

### `group_run`

`id PK`、`group_session_id FK`、`run_id FK UNIQUE`、`reporter_agent_id FK`、`summary_epoch BIGINT DEFAULT 0`、`delivered_summary_epoch BIGINT DEFAULT 0`、`version`。`summary_epoch` 是已分配的最新汇总 epoch，是唯一单调计数器；是否已请求由 `group_summary_request(group_run_id,summary_epoch)` 唯一行表示，不再保存第二个 requested epoch 镜像。

禁止增加 `status`、`outcome`、`wait_reason` 或 `completed_at`；这些字段只在一对一 Run 中存在。

### `group_run_member`

`group_run_id FK`、`agent_id FK`、`agent_version_id FK NULL`、`participation_status available|unavailable`、`unavailable_reason NULL`、`context_scope_json`、`capability_boundary_json`、`version`；PK `(group_run_id,agent_id)`。Reporter 可用性变更在锁定成员快照后递增 `version`，供失败投影和 CAS 诊断使用。

Reporter 必须是本快照中 available 的成员。唯一 `(group_run_id,agent_version_id)` 不成立，因为同一版本不应限制多逻辑用途；按 Agent 主键即可。

### `group_summary_request`

`id PK`、`group_run_id FK`、`summary_epoch`、`reporter_work_item_id FK`、`trigger_event_id FK`、`status queued|running|delivered|superseded|failed`、`final_message_id FK NULL`、`version`；唯一 `(group_run_id,summary_epoch)`。

它是防重复唤醒和诊断记录，不是第二套 Run lifecycle。

### `group_diagnostic`

`id PK`、`session_id FK`、`run_id FK`、`group_session_id FK`、`group_run_id FK`、`diagnostic_code`、`category delegation_guard|progress_guard|quiescence|delivery`、`severity info|warning|error`、`disposition observed|warned|blocked|recovered`、`source_entity_type`、`source_entity_id`、`source_event_id FK NULL`、`caller_agent_id FK NULL`、`target_agent_id FK NULL`、`dedupe_key`、`causal_fingerprint CHAR(64)`、`causal_chain_json`、`limit_snapshot_json`、`safe_details_json`、`summary`、`created_at`；唯一 `(run_id,dedupe_key)`，索引 `(group_run_id,created_at)` 和 `(run_id,diagnostic_code,created_at)`。

该表只追加，MySQL trigger 拒绝 UPDATE/DELETE。它记录 Runtime 已作出的有界调度决定，不保存原始委托正文、Prompt、Chain-of-Thought 或敏感 Tool 参数；正文只进入不可逆 request fingerprint。已被安全拦截的重复、回环、最大深度和扇出尝试仍作为诊断展示，但不单独把成功交付的 Run 标记为 `completed_with_errors`。真实 WorkItem/AgentInvocation 失败、超时或不可用仍进入失败结算。

### `shared_task`

`shared_task`：`id PK`、`group_session_id FK`、`group_run_id FK NULL`、`title`、`description`、`status pending|in_progress|completed|blocked|cancelled`、`assignee_agent_id FK NULL`、`version`。只有明确 assignee 且 in_progress 的任务进入 quiescence pending 判断；它不自动创建 WorkItem。

- `shared_task_dependency`：`shared_task_id FK`、`predecessor_task_id FK`、`sort_order`；PK `(shared_task_id,predecessor_task_id)`，CHECK 禁止自依赖。领域事务锁定相关任务，确保两端属于同一 GroupSession 且新边不成环；该边只影响提示/UI，不触发调度、授权或 quiescence。
- `shared_task_result_ref`：`shared_task_id FK`、`ref_type artifact|message|verification`、`artifact_id FK NULL`、`message_id FK NULL`、`verification_result_id FK NULL`、`ref_key GENERATED`、`sort_order`；PK `(shared_task_id,sort_order)`，唯一 `(shared_task_id,ref_type,ref_key)`，CHECK 要求与 ref_type 对应的 FK 恰好一个非空；领域校验引用属于同 tenant 且对该 Group 可见。

### `usage_price_snapshot`、`usage_ledger_entry`

- `usage_price_snapshot`：`id PK`、`subject_type model|tool|sandbox|storage|egress`、`provider NULL`、`subject_key`、`metric`、`unit`、`currency CHAR(3)`、`price_per_unit DECIMAL(30,12)`、`price_status estimated|contracted`、`effective_from/to`、`source_label`、`content_hash UNIQUE`、`created_by_account_id FK`、`created_at`。同一 subject/provider/metric/currency 的生效窗口不得重叠；新价格创建新快照，不能修改旧快照。
- `usage_ledger_entry`：`id PK`、当前 `account_id` 以及 Session/Run/GroupRun/WorkItem/Runner/AgentVersion/AgentTurn/ProviderAttempt/ToolInvocation/ToolAttempt 归因 FK、`source_type`、`source_id`、`source_operation_key`、`metric`、`unit`、`quantity DECIMAL(30,9)`、`measurement_status measured|estimated|unknown`、绑定的 `price_snapshot_id/currency/unit_price_snapshot/cost_amount`、`cost_status estimated|contracted|unknown`、`adjusts_entry_id FK NULL`、`safe_metadata_json`、`created_at`；唯一 `(source_operation_key,metric)`。

Provider 的每次成功、失败、重试或 `outcome_unknown` Attempt 都会生成请求、Token 与活跃时间条目；Sandbox ToolAttempt 生成 Tool unit 与 Sandbox 时间条目。缺少可信计量或生效价格时必须保存 `unknown`，不得以 0 成本代替。纠错只能追加 `source_type=adjustment` 的正/负条目并引用原条目。两张表均由 MySQL trigger 禁止 UPDATE/DELETE；v0.3.0 Tenant 扩展会在保留当前 account 归因的同时补入非空 tenant authority。

### `agent_eval_suite*`、`agent_eval_run*`

- `agent_eval_suite` 是 Agent 下稳定的发布门禁标识，保存 active/inactive 与当前版本指针；`agent_eval_suite_version` 和 `agent_eval_case` 冻结名称、阈值、逐用例输入/权重/断言及内容哈希，由 MySQL trigger 禁止 UPDATE/DELETE。修改套件只能发布新版本。
- `agent_eval_run` 绑定一个 `draft_test_snapshot` 的精确 `candidate_content_hash`、一个不可变 SuiteVersion 和门禁阈值快照；`agent_eval_case_run` 一对一引用真实 Session/Run，维护 Worker 在 Run 终态后读取最终 Message，并结算文本、长度与 Usage 账本成本断言。
- 发布事务锁定当前 active Suite，逐个要求存在 `suite_version_id + candidate_content_hash + passed` 的终态 EvalRun。草稿内容、Model/Tool/Skill/Policy/Sandbox 当前版本或评测套件版本任一变化，都会改变匹配条件，旧成绩不会误放行新版本。

### `verification_result`

`verification_result`：`id PK`、`run_id FK`、`group_run_id FK NULL`、`rule_key`、`rule_version`、`verification_epoch BIGINT`、`status passed|failed|insufficient_evidence`、`fact_fingerprint CHAR(64)`、`observed_facts_json`、`missing_facts_json`、`created_at`；唯一 `(run_id,rule_key,rule_version,verification_epoch)` 和 `(run_id,rule_key,rule_version,fact_fingerprint)`。epoch 在锁定 `run_id + rule_key + rule_version` 后单调分配；完全相同的权威事实幂等返回旧结果，新事实才创建新 epoch，旧结果不覆盖。

- `verification_evidence_ref`：`verification_result_id FK`、`evidence_type tool_attempt|artifact|workspace_checksum|business_fact`、`tool_attempt_id FK NULL`、`artifact_id FK NULL`、`workspace_entry_id FK NULL`、`workspace_generation NULL`、`external_fact_ref NULL`、`checksum CHAR(64) NULL`、`evidence_key GENERATED`、`sort_order`；PK `(verification_result_id,sort_order)`，唯一 `(verification_result_id,evidence_type,evidence_key)`。CHECK 要求类型对应的引用恰好一个非空，workspace/business_fact 必须有 checksum，workspace 还必须固定 generation。领域服务校验数据属于同 tenant/Run 且对当前 account/Group 可见；结果不由模型文字直接写入。

### `run_progress_mark`、`late_result_observation`

- `run_progress_mark`：`id PK`、`run_id FK`、`work_item_id FK`、`runner_id FK`、`agent_turn_id FK UNIQUE`、`made_progress`、`progress_fingerprint`、`reason_code`、`consecutive_no_progress`、`created_at`；只追加，只用于预算诊断和给 Agent 的有界反馈，不能结算 Run；
- `late_result_observation`：`id PK`、`run_id FK`、`work_item_id FK NULL`、`source_type tool_attempt|provider_attempt|agent_invocation`、`source_id`、`original_status`、`result_hash`、`safe_result_json`、`observed_at`；唯一 `(source_type,source_id,result_hash)`。它只保存脱敏摘要与 stdout/stderr hash，绝不覆盖原 Invocation、唤醒 WorkItem 或修改终态 Run。

Verification、EvidenceRef、ProgressMark 和 LateResultObservation 由 MySQL trigger 同时禁止 UPDATE 与 DELETE。Run、ToolInvocation 和 ToolAttempt 的终态也由 trigger 禁止后续 UPDATE，应用层 CAS/fencing 之外再提供数据库终态保护。

## 9. Workspace 与 Artifact

### `session_sandbox`、`workspace_entry`

- `session_sandbox`：`id PK`、`session_id FK UNIQUE`、`sandbox_profile_id FK`、`sandbox_profile_version_id FK`、`workspace_protocol_version`、`artifact_protocol_version`、`mount_policy_hash`、`root_storage_key`、`status provisioning|ready|failed|retained|deleted`、`version`；
- `workspace_entry`：`id PK`、`session_sandbox_id FK`、`logical_root private_agent|group_shared`、`owner_agent_id FK NULL`、`group_session_id FK NULL`、`owner_scope_key GENERATED`、`relative_path`、`size_bytes`、`checksum`、`generation`、`version`。CHECK 要求 private_agent 只设置 owner_agent_id，group_shared 只设置 group_session_id；`owner_scope_key` 分别编码 `agent:<id>` 或 `group:<id>`。

唯一 `(session_sandbox_id,logical_root,owner_scope_key,relative_path)`。每次访问还需 ExecutionPrincipal 校验，数据库路径不是授权替代。

### `runner_sandbox_instance`

`id PK`、`runner_id FK UNIQUE`、`session_sandbox_id FK`、`sandbox_profile_version_id FK`、`provider_instance_id NULL`、`status provisioning|running|stopped|failed|lost`、`mount_manifest_hash`、`egress_policy_hash`、`resource_policy_hash`、`started_at NULL`、`ended_at NULL`、`failure_code NULL`、`version`；唯一 nullable `(provider_instance_id)`。

创建实例前校验 Runner 的 AgentVersion snapshot 与 SessionSandbox protocol/mount policy 兼容；实例只能挂载 ExecutionPrincipal 允许的逻辑根。实例停止/丢失不删除 SessionSandbox，恢复 Runner 必须创建新的 instance 记录，不能复活旧 VM/容器身份。

### `workspace_lock_request`

`id PK`、`session_sandbox_id FK`、`scope_key`、`mode read|write`、`ticket BIGINT`、`status queued|granted|released|expired|cancelled`、`lease_owner NULL`、`lease_token NULL`、`lease_expires_at NULL`、`fencing_token BIGINT NULL`、`tool_invocation_id FK`、`version`；唯一 `(session_sandbox_id,scope_key,ticket)`，索引 `(session_sandbox_id,scope_key,status,ticket)` 和 `(status,lease_expires_at)`。

ticket 在锁定 SessionSandbox/独立 counter 后单调分配。read 只有在前方无 write 且无 active writer 时批量 grant；write 只有在前方无请求且无 active reader/writer 时 grant。每次文件读写同时验证 lease_token；写操作还验证递增 fencing_token。等待锁的 ToolInvocation 为 waiting_resource，不占 Runner。

### `artifact`

`id PK`、`tenant_id FK`、`owner_account_id`、`session_id FK`、`group_session_id FK NULL`、`source_relative_path`、`immutable_storage_key UNIQUE`、`name`、`mime_type`、`size_bytes NULL`、`checksum CHAR(64) NULL`、`visibility private|direct|group|user`、`created_by_agent_id FK`、`work_item_id FK`、`runner_id FK`、`tool_invocation_id FK`、`parent_artifact_id FK NULL`、`provenance_json`、`status publishing|published|unavailable|deleted`、`availability_reason NULL|publish_failed|storage_fault|checksum_mismatch|deletion_pending|retention_expired`、`published_at NULL`、`deletion_requested_at NULL`、`deleted_at NULL`、`version`；复合 FK `(tenant_id,owner_account_id) → platform_account(tenant_id,id)`。

CHECK 固定为：publishing 的 size/checksum/reason/published/deletion/deleted 均为空；published 的 size/checksum/published_at 非空且 reason/deletion/deleted 为空；unavailable/deleted 的 reason 非空，且 deleted_at 当且仅当 status=deleted 时非空。reason=publish_failed 时 published_at/deletion_requested_at 必须为空、size/checksum 允许为空；其他 reason 必须保留非空 size/checksum/published_at。reason=deletion_pending|retention_expired 必须有 deletion_requested_at。合法 reason 迁移、恢复前置和 Event 以状态机为准。

首次 `published` 后禁止修改 storage key、source path、name/mime/bytes metadata/checksum、tenant/owner/session/group/visibility、creator/WorkItem/Invocation、parent、provenance 和 published_at。新内容或新元数据版本都创建新 Artifact；Retention/受控恢复只能按状态机修改 status、availability/deletion 字段与治理 version，并写审计。恢复为 published 必须验证同一 immutable key 与原 checksum，不能改变任何不可变字段。

## 10. Channel 与通知（启用 Channel 时必需）

### `channel_plugin`、`channel_account`

- `channel_plugin`：`id PK`、`key UNIQUE`、`name`、`implementation_ref`、`implementation_digest`、`signature_scheme`、`config_schema_json`、`required_conformance_suite_version`、`current_conformance_run_id FK NULL`、`status active|inactive`、`version`；`current_conformance_run_id` 只能指向该 Plugin 且 suite 匹配的 terminal passed ConformanceRun，结果历史不回写 Plugin；
- `channel_account`：`id PK`、`tenant_id FK`、`owner_account_id`、`channel_plugin_id FK`、`external_account_id`、`active_external_account_key GENERATED NULL|external_account_id`、`credential_ref`、`status active|revoked|error`、`version`；复合 FK `(tenant_id,owner_account_id)`，唯一 `(channel_plugin_id,active_external_account_key)`，active/error 共享当前 key，revoked 后允许新授权创建新行。

### `agent_channel_binding`、`channel_identity_binding`、`channel_conversation_binding`

- `agent_channel_binding`：`id PK`、`channel_account_id FK`、`entry_agent_id FK`、`status active|inactive`、`active_channel_account_key GENERATED NULL|channel_account_id`、`version`；唯一 `(active_channel_account_key)`，使一个 ChannelAccount 最多一个 active binding 且保留 inactive 历史；
- `channel_identity_binding`：`id PK`、`tenant_id FK`、`channel_account_id FK`、`external_user_subject`、`account_id`、`verification_method`、`verified_at`、`status active|revoked`、`active_subject_key GENERATED NULL|external_user_subject`、`version`；复合 FK `(tenant_id,account_id)`，唯一 `(channel_account_id,active_subject_key)`，保留 revoked 历史；
- `channel_conversation_binding`：`id PK`、`channel_account_id FK`、`external_conversation_id`、`session_id FK UNIQUE`、`status active|closed`、`active_conversation_key GENERATED NULL|external_conversation_id`、`version`；唯一 `(channel_account_id,active_conversation_key)`，显式“新会话”关闭旧绑定后可为同一外部 conversation 创建新 Session 绑定。

Provider 签名验证成功后才能查询 identity binding。外部 display name、消息正文或裸 userId 不能创建身份绑定；绑定创建/撤销写 PlatformAuditEvent。

### `notification_delivery`

`notification_delivery`：`id PK`、`session_id FK`、`execution_event_id FK`、`channel_conversation_binding_id FK`、`dedupe_key`、`provider_idempotency_key`、`status pending|running|succeeded|failed|dead|outcome_unknown`、`attempt_count`、`next_attempt_at NULL`、`provider_delivery_id NULL`、`last_error_code NULL`、`version`；唯一 `(channel_conversation_binding_id,dedupe_key)` 和 `(channel_conversation_binding_id,provider_idempotency_key)`，索引 `(status,next_attempt_at,id)`。`provider_idempotency_key` 在同一 Delivery 所有尝试中稳定，不包含正文。

- `notification_delivery_attempt`：`id PK`、`notification_delivery_id FK`、`attempt_no`、`status running|succeeded|failed|outcome_unknown`、`lease_owner`、`lease_token`、`lease_expires_at`、`provider_request_id NULL`、`provider_delivery_id NULL`、`error_code NULL`、`started_at`、`ended_at NULL`、`version`；唯一 `(notification_delivery_id,attempt_no)`，索引 `(status,lease_expires_at)`。Attempt 终态后只追加诊断，不覆盖请求/回执事实。

Delivery 与触发它的领域 Event/Outbox 同事务创建；投递失败只改变 NotificationDelivery 并追加安全 Event，不改变原 Run。Provider 不支持幂等/状态查询且请求可能已送达时必须进入 outcome_unknown，禁止自动再投。Channel 未启用时这些表可以为空，但不能用另一套临时表替代。

## 11. Event、Outbox 与审计

### `execution_event`

`id PK`、`tenant_id FK`、`session_id FK`、`run_id FK NULL`、`sequence BIGINT`、`event_type`、`visibility private|direct|group|user`、`actor_kind user|agent|runtime|tool|system`、`actor_account_id FK NULL`、`actor_agent_id FK NULL`、`actor_runtime_id NULL`、`aggregate_type`、`aggregate_id`、`aggregate_version`、`cause_type`、`cause_id`、`dedupe_key`、`safe_summary`、`payload_json`、`hash_schema_version SMALLINT NOT NULL DEFAULT 1`、`previous_event_hash CHAR(64) NULL`、`event_hash CHAR(64)`、`occurred_at`。CHECK 要求 actor_kind 对应且只对应一个可用 actor 字段；system 可全部为空。写入前 payload_json 必须通过 OpenAPI `EventPayload` v1，不能存任意领域对象或正文。sequence=1 时 previous 为空，其他必须等于同 Session 前一 Event hash。

唯一 `(session_id,sequence)`、`(session_id,dedupe_key)`、`(aggregate_type,aggregate_id,aggregate_version,event_type)`；索引 `(run_id,sequence)`、`(aggregate_type,aggregate_id,sequence)`。sequence 在 Session 行的 `next_event_sequence` 中事务分配。

### `outbox_command`

`id PK`、`tenant_id FK NULL`、`topic runtime.control|runtime.runner|runtime.tool|runtime.sandbox|runtime.delivery|runtime.maintenance`、`command_type`、`aggregate_type`、`aggregate_id`、`partition_key`、`priority SMALLINT`、`cause_event_id FK NULL`、`dedupe_key`、`payload_json`、`status pending|claimed|retry_wait|completed|dead`、`available_at`、`claim_owner NULL`、`claim_token NULL`、`claim_expires_at NULL`、`attempt_count`、`last_error_code NULL`、`completed_at NULL`、`version`。

唯一 `(topic,dedupe_key)`；索引 `(topic,status,available_at,id)`、`(status,claim_expires_at)`。领域状态、ExecutionEvent 和必要 Outbox 必须同事务提交。

### `platform_audit_event`

`id PK`、`tenant_id FK`、`audit_sequence BIGINT`、`actor_account_id FK NULL`、`actor_external_identity_id FK NULL`、`actor_api_key_id FK NULL`、`asserted_user_id_snapshot NULL`、`category`、`action`、`resource_type`、`resource_id`、`outcome succeeded|denied|failed`、`request_id`、`dedupe_key`、`safe_summary`、`changes_redacted_json`、`hash_schema_version SMALLINT NOT NULL DEFAULT 1`、`previous_event_hash CHAR(64) NULL`、`event_hash CHAR(64)`、`occurred_at`，只追加；唯一 `(tenant_id,audit_sequence)` 和 `(dedupe_key)`。锁定 Tenant `next_audit_sequence` 分配序号并构建与 ExecutionEvent 同样的 canonical hash chain。

### `ledger_integrity_seal`

`id PK`、`tenant_id FK`、`ledger_type platform_audit|session_execution`、`scope_id`、`through_sequence`、`hash_schema_version SMALLINT`、`head_hash CHAR(64)`、`signature_algorithm`、`kms_key_version`、`signature`、`immutable_object_uri`、`sealed_at`，只追加；唯一 `(tenant_id,ledger_type,scope_id,through_sequence)`。`platform_audit` 的 scope_id 必须等于 tenant_id，`session_execution` 的 scope_id 必须指向同 Tenant 的 session_id，由写服务和数据库 Trigger 双重校验。签名对象固定包含 `tenant_id + ledger_type + scope_id + through_sequence + hash_schema_version + head_hash + sealed_at`，避免合法链头被替换到其他范围；对象 key 为 `ledger-seals/<tenant_id>/<ledger_type>/<scope_id>/<through_sequence>.json`，开启版本锁/对象锁且禁止覆盖。Production Maintenance 至少每日验证 hash chain，用非数据库内的 KMS 签名该对象并写入不可覆盖对象存储；签名或链校验失败是安全事件，不允许“重建”历史掩盖差异。

`hash_schema_version=1` 的计算合同固定为：对该行除 `previous_event_hash`、`event_hash` 外的全部只追加列构造 JSON 对象，UUID 使用小写连字符格式，时间使用 UTC 六位小数和 `Z`，JSON 数字保留数据库整数值，嵌套 JSON 先按 RFC 8785 JCS 规范化；随后计算 `SHA-256(0x01 || UTF-8(JCS(row)) || 0x00 || previous_hash_bytes)`。第一行的 `previous_hash_bytes` 为 32 个零字节，后续行使用前一行 `event_hash` 的 32 字节解码值。升级算法只能新增 `hash_schema_version`，旧行、旧 seal 和旧算法验证器必须保留；不得原地重算历史 hash。

Registry、身份、角色、密钥、Retention、break-glass 和策略变更写平台审计；Session 中 Agent 执行写 ExecutionEvent。跨账本关联使用 request/cause ID，不复制敏感正文。

## 12. 约束实施分层

不得用“应用会注意”替代可由数据库表达的约束，也不得用复杂 Trigger 隐藏跨聚合业务流程。约束归属固定如下。

### DDL：FK / UNIQUE / CHECK / generated column

1. 所有内部账号关系使用 `account_id`；所有 tenant-owned 子表物理带 tenant_id，tenant-owned FK 使用复合 FK，迁移测试必须证明数据库直接拒绝跨 Tenant parent/account/session/agent 组合；
2. GroupRun 的 `run_id UNIQUE NOT NULL`，表中物理上不存在 lifecycle/outcome 字段；
3. `(work_item_id,execution_epoch)`、`(group_run_id,summary_epoch)`、Tool operation、AgentInvocation operation、Event/Outbox dedupe key 唯一；
4. status、mode、visibility、actor/owner 字段组合使用 CHECK；
5. active Draft、WorkingPlan head和 Membership 使用 nullable generated key + UNIQUE；Clarification/Approval 使用 idempotency/subject UNIQUE 与结算 CAS；
6. Artifact storage key、AgentCard version、Session event sequence 和版本序号唯一；
7. 所有 required FK 默认 RESTRICT，历史执行/版本/审计不级联删除。

### 数据库 Trigger + 独立 migration role

应用运行账号不能禁用或修改 Trigger。Trigger 只承担行级不可变规则：

- `agent_version` 为 published/deprecated/draft_test_snapshot 时拒绝行为内容字段 UPDATE 和整行 DELETE；只允许 `published→deprecated` 的状态/CAS/审计列变化，对应 Tool/Skill/Card 关系拒绝 UPDATE/DELETE；
- `skill_version`、`policy_version`、`sandbox_profile_version` 拒绝内容列 UPDATE 和整行 DELETE，只允许稳定状态机中的 `status + version + status timestamp/actor` CAS 变化；
- Artifact 为 published 后拒绝修改全部内容/归属/来源列；只允许状态机中的 status、availability/deletion 字段和治理 version CAS 变化；
- append-only Event/Audit/Attempt/PlanUpdate/Checkpoint 拒绝 UPDATE/DELETE；Attempt 只允许由当前非终态向合法终态更新的状态列，不允许覆盖请求/结果事实；
- Run、WorkItem、Runner、Invocation 的终态不得迁回非终态；
- 只有专用 migration/retention role 能执行已批准的结构迁移或 Retention tombstone，普通应用账号无 DDL 权限。

Trigger 必须版本化进入 Alembic，并有正向和拒绝测试；不能只存在于手工生产数据库。

### 领域事务 + CAS / fencing

以下跨行/跨表事实由唯一领域服务在显式事务中保证，并由数据库条件更新与锁验证：

- `work_item.active_runner_id` 必须指向同 WorkItem、同当前 execution_epoch 的唯一非终态 Runner；创建/清除只经过 RunnerCoordinator；
- Runner/ToolAttempt 写入必须匹配 `status + execution_epoch + lease_token`；条件更新 0 行即 fencing reject；
- Clarification requester 必须是 Run 所属 Session account；target account 可以不同，但必须同 Tenant、active 且在创建与回答时均属于当前 Agent 可触达集合。回答 target 不因此获得源 Session 权限；
- Approval target 可以不同于 Run 所属 Session account，但权限型审批必须由资源策略计算合法 approver，并将 grantor、beneficiary 和精确 subject scope 分离；
- ExternalIdentity.principal_type 必须与 PlatformAccount.account_type 相同；service account 不能成为自然人 Approval/Clarification 的 target；
- Group Reporter 必须是本 GroupRun available member；GroupRun 与 Run 必须属于同一 Session 链；
- Tool/AgentInvocation/Outbox 相同业务 key 的 request hash 不同必须返回冲突；
- Artifact DB 元数据与对象存储提交由 Reconciler 依据 artifact_id/checksum 收敛；
- 用户数据所有查询与写入校验 tenant、内部 account 和父 Session 归属；外部 `asserted_user_id` 绝不直接参与领域 FK。

每项领域不变量必须有至少两个并发事务的竞争测试和故障注入测试，不能只覆盖单线程 happy path。

## 13. 最低索引与容量审查

上线前必须用代表性数据量验证以下热路径的执行计划：

- 待领取 Outbox：`topic + status + available_at`；
- 租约回收：`status + lease_expires_at`；
- 用户 Session 列表：`tenant + account + updated_at`；
- Session/Run 事件续读：`session/run + sequence`；
- AgentCard 发现：`tenant + visibility + published/group_enabled + tags`；
- 等待动作：`tenant + target_account + status + created_at`；
- Group quiescence：`run/work_item/invocation/approval/shared_task status`；
- Artifact 列表和下载授权：`tenant + owner_account + session + created_at`。
- KnowledgeBase/文档列表：`tenant + owner_account/status`、`tenant + knowledge_base/status`；检索：MySQL `tenant + index_revision + ordinal`，Qdrant `tenant_id + index_revision_id` payload index；异步索引领取：`topic=knowledge.index + status + available_at`。

不得用新增 Redis 权威状态掩盖缺失索引；具体容量、SLO 和 HA Gate 见[生产就绪要求](../operations/production-readiness.md)。
