# 第一阶段数据库 Schema

本文是第一阶段物理关系模型的唯一实现级定义。表名、字段名使用 `snake_case`；产品对象语义以 [权威模块](../README.md) 为准。

## 1. 全局约定

- MySQL 8、InnoDB、`utf8mb4_0900_ai_ci`；生产连接显式设为 `READ COMMITTED`。
- 主键均为应用生成 UUIDv7 `CHAR(36)`；外部 `user_id` 为企业 canonical userId，`VARCHAR(191)`，不得与账号主键混用。
- 时间为 UTC `DATETIME(6)`；所有表含 `created_at`，可变表含 `updated_at`。
- 可并发聚合含 `version BIGINT NOT NULL DEFAULT 1`，条件更新成功后 `version = version + 1`。
- 状态列使用受代码枚举和数据库 `CHECK` 双重约束的 `VARCHAR`，避免 MySQL ENUM 迁移锁定。
- 秘密只存 `secret_hash` 或 Secret Manager 的 `credential_ref`；不存明文。
- JSON 仅用于 JSON Schema、不可变参数/结果信封、执行快照、扩展元数据；核心关系必须使用关联表。
- 用户业务表冗余 `user_id` 用于所有权过滤。关键父表增加 `(id,user_id)` 唯一键，子表使用复合 FK 保证父子 `user_id` 一致；无法使用复合 FK 的多父关系由锁定事务校验并在一致性扫描中复核。

## 2. 账号、角色与 API Key

### `user_identity` 与 `platform_account`

`user_identity` 保存不会因账号改绑而消失的数据身份：`user_id VARCHAR(191) PRIMARY KEY`、`status active/orphaned`、`created_at`。Session、Memory、Plan、Group、Execution 与 Artifact 的 `user_id` FK 指向该表。改绑时旧 identity 置 orphaned（仍保留数据），创建或取得新 identity，再改变账号指针；不迁移业务行。

| 字段 | 类型/约束 | 职责 |
| --- | --- | --- |
| `id` | PK | 平台账号外壳 ID |
| `user_id` | `VARCHAR(191) NOT NULL UNIQUE FK user_identity` | 账号当前 canonical userId 指针 |
| `email`、`display_name` | nullable/非空展示字段 | 登录资料，不作为数据归属键 |
| `password_hash` | nullable | 本地登录；联登账号可为空 |
| `status` | `active/discarded/disabled` | `discarded` 用于 userId 改绑后的旧外壳 |
| `auth_revision` | BIGINT | 密码、角色或身份变更后令旧 Token 失效 |
| `version`、审计时间 | | 乐观并发与审计 |

索引：`UNIQUE(user_id)`、`UNIQUE(email)`（非空部署规则）、`INDEX(status, created_at)`。`user_id` 更新通过身份服务专用事务完成，不迁移旧数据；若目标已属于既有账号则拒绝复制并要求登录既有账号。

### `platform_account_role`

`account_id FK`、`role_key`（`user/builder/admin`）、`granted_by_account_id FK`、`granted_at`，PK `(account_id, role_key)`。创建账号必须插入 `user`；`builder/admin` 是附加角色，授权判断取并集。

### `agent_api_key`

`id PK`、`agent_id FK`、`name`、`caller_label`、`prefix UNIQUE`、`secret_hash UNIQUE`、`status active/revoked/expired`、`created_by_account_id FK`、`expires_at`、`last_used_at`、`rotated_from_id FK NULL`、`version`。索引 `(agent_id,status)`、`(expires_at,status)`。Key 删除是撤销，不物理删除；审计至少保留 365 天。

### `idempotency_record`

`id PK`、`principal_type`、`principal_id`（Web 用 userId，API 用 api_key_id）、`operation`、`idempotency_key`、`request_hash`、`resource_type`、`resource_id`、`http_status`、`response_snapshot JSON`、`expires_at`。唯一 `(principal_type,principal_id,operation,idempotency_key)`；同 Key 不同 hash 返回 `409 idempotency_mismatch`。默认保留 7 天，创建 Run/Invocation 的资源引用不得随记录清理。

## 3. Agent 与装配关系

### `agent`

`id PK`、`owner_account_id FK`、`visibility private/enterprise`、`status draft/published/inactive`、`draft_version_id FK NULL`、`current_published_version_id FK NULL`、`next_version_no INT NOT NULL DEFAULT 1`、`version`、审计时间。`draft_version_id` 只允许在创建事务回填前短暂为 NULL，任何已提交 Agent 必须有 draft；MySQL 无 deferred FK，此跨行不变量由唯一的创建服务事务保证并由一致性扫描复核。稳定身份只承载所有权、访问范围和生命周期；名称等可执行发现信息来自相应 AgentVersion。索引 `(owner_account_id,status)`、`(visibility,status)`。

### `agent_version`

`id PK`、`agent_id FK`、`version_no INT NULL`、`publication_status draft/published/deprecated/draft_test_snapshot`、`draft_revision BIGINT NULL`、`derived_from_version_id FK NULL`、`name VARCHAR(200)`、`description TEXT`、`instructions MEDIUMTEXT`、`model_id FK`、`sandbox_profile_id FK`、四个 `policy_definition_id FK`、`group_enabled BOOL`、`created_by_account_id FK`、`published_at`、`content_hash CHAR(64)`、`immutable_at NULL`、审计时间。Published/deprecated 必须有 `version_no`，draft 与 test snapshot 必须为 NULL；唯一 `(agent_id,version_no)`。生成列 `draft_agent_id = CASE WHEN publication_status='draft' THEN agent_id ELSE NULL END` 加 `UNIQUE(draft_agent_id)`，保证一个 Agent 最多一个可编辑 draft。`draft_test_snapshot` 是受控测试 Run 创建时复制的不可变快照，不可发布、绑定或作为正式调用目标。

关系表：

- `agent_version_tool(agent_version_id, platform_tool_id, sort_order)`，PK 前两列，`UNIQUE(agent_version_id,sort_order)`。
- `agent_version_skill(agent_version_id, skill_id, sort_order)`，同上，绑定逻辑 Skill。
- `agent_version_binding(agent_version_id, bound_agent_id, sort_order)`，禁止自绑定，PK 前两列。

Agent 创建事务插入 Agent 和一行 `draft` AgentVersion，draft 的 `version_no=NULL,draft_revision=1`，再回填 `draft_version_id`；这是唯一允许的 Agent/AgentVersion 循环创建流程。发布时锁 Agent 与当前 draft，完整校验后把 draft 标量和关系行**复制**为一行新的 `published` AgentVersion，分配 `next_version_no`，更新 `current_published_version_id/next_version_no`；原 draft 行不转态、不换 ID，继续作为内容相同的下一工作草稿，只更新 `derived_from_version_id` 和递增 `draft_revision`。因此事务中从不出现第二个 draft，也不覆盖历史版本。首次发布把 Agent `draft→published`；inactive Agent 发布新版仍保持 inactive，重新启用必须单独命令。

Published、deprecated 与 draft_test_snapshot 的内容不可变性由 `immutable_at` sealing 保护。复制事务先插入目标版本（immutable_at=NULL）、复制关系、计算 content_hash，最后执行 seal 更新设置 immutable_at/published_at；`BEFORE UPDATE/DELETE` trigger 对已 sealed 行只允许 `publication_status: published→deprecated` 这一治理元数据迁移，拒绝任何装配字段、hash、version_no 或删除变更，关系表 trigger 在父版本 sealed 后拒绝 INSERT/UPDATE/DELETE。任何未 sealed 的非 draft 行都不能被执行选择，且只存在于未提交发布事务；服务层仍先校验并返回领域错误，trigger 是最终防线。发布失败整事务回滚，Agent 指针、next_version_no、draft_revision 和复制行均不变化。Draft Test 在创建 Run 的事务中以同样流程复制并 seal `draft_test_snapshot`，WorkItem 固定该快照；测试快照在关联 Run 终态 30 天且无其他引用后可清理。

## 4. Registry

### `model`

`id PK`、`key UNIQUE`、`provider`、`model_name`、`endpoint`、`credential_ref`、`default_parameters JSON`、`parameter_version INT`、`capabilities JSON`、`enabled BOOL`、`config_version BIGINT`、`created_by_account_id FK`、审计时间。索引 `(provider,enabled)`。参数 JSON 只含 Provider 参数；Agent 不覆盖。

### `platform_tool`

`id PK`、`tool_key UNIQUE`、`display_name`、`description`、`input_schema JSON`、`output_schema JSON`、`implementation_ref`、`implementation_version`、`execution_mode inline/durable_async`、`enabled BOOL`、`confirmation_policy never/always`、`platform_config JSON`、`config_version BIGINT`、审计时间。代码注册字段的 hash 存 `registration_hash`；管理面只能改展示字段、enabled、policy 与实现声明为可编辑的平台配置。索引 `(enabled,tool_key)`。

### `skill` 与 `skill_version`

`skill`：`id PK`、`logical_name UNIQUE`、`name`、`description`、`owner_account_id FK`、`status active/inactive`、`current_published_version_id FK NULL`、`version`。

`skill_version`：`id PK`、`skill_id FK`、`version_no INT`、`status uploaded/validating/published/rejected/deprecated`、`package_uri`、`checksum CHAR(64)`、`manifest_name`、`manifest_description`、`validation_error TEXT NULL`、`created_by_account_id FK`、`published_at`。唯一 `(skill_id,version_no)`、`(skill_id,checksum)`；相同包可由不同逻辑 Skill 独立发布，但同一逻辑 Skill 不重复建版本。索引 `(skill_id,status,version_no DESC)`。

### `policy_definition`

`id PK`、`policy_key UNIQUE`、`policy_type context/history/memory_write/memory_recall`、`name`、`description`、`implementation_ref`、`implementation_version`、`default_parameters JSON`、`parameter_version INT`、`enabled BOOL`。索引 `(policy_type,enabled)`。第一阶段 Registry seed 只包含权威列出的九个 key。

### `sandbox_profile`

`id PK`、`profile_key UNIQUE`（首批 `local`）、`type`、`description`、`implementation_ref`、`config JSON`、`config_version`、`enabled`。宿主路径不进入 AgentVersion。

## 5. Session、Message、Run

### `user_session`

`id PK`、`user_id FK user_identity`、`entry_agent_id FK`、`status active/closed/archived`、`title`、`origin_type web/api/channel`、`origin_api_key_id FK NULL`、`origin_channel_account_id FK NULL`、`origin_external_conversation_id NULL`、`next_event_sequence BIGINT DEFAULT 1`、`version`、`closed_at`。`session_sandbox` 通过唯一 `session_id` 反向一对一关联，避免循环外键。索引 `(user_id,status,updated_at DESC)`、`(entry_agent_id,created_at)`；Channel 绑定另设唯一表，不依赖 nullable unique。

### `message`

`id PK`、`session_id FK`、`user_id`、`scope_type chat/group`、`group_session_id FK NULL`、`group_run_id FK NULL`、`sender_type user/agent/system`、`sender_user_id NULL`、`sender_agent_id FK NULL`、`recipient_type session/group_node/user`、`recipient_group_node_id FK NULL`、`role user/assistant/system/tool_notice`、`content MEDIUMTEXT`、`content_format text/markdown`、`status committed/superseded/redacted`、`run_id FK NULL`、`work_item_id FK NULL`、`clarification_id FK NULL`、`reply_to_message_id FK NULL`、`caused_by_event_id FK NULL`。索引 `(session_id,created_at,id)`、`(group_session_id,recipient_group_node_id,created_at)`、`(run_id,created_at)`。附件用 `message_artifact(message_id,artifact_id,visibility)`。

### `run`

`id PK`、`session_id FK`、`user_id`、`mode chat/plan/group`、`execution_kind normal/draft_test/proposal_generation`、`lifecycle running/waiting/settling/completed/cancelled`、`wait_reason NULL/waiting_user/waiting_agent/waiting_tool/waiting_resource/graph_confirmation`、`outcome NULL/succeeded/partially_succeeded/failed`、`entry_agent_id FK`、`source_run_id FK NULL`、`result_summary TEXT NULL`、`error_code NULL`、`cancellation_requested_at NULL`、`version`、`started_at`、`finished_at`。CHECK 要求 lifecycle=waiting 时 wait_reason 非空，其他 lifecycle 除过渡事务外提交时必须为 NULL；唯一 `(id,user_id)` 支持子表复合 FK；索引 `(session_id,created_at)`、`(user_id,lifecycle,updated_at)`。`draft_test` 只固定 immutable draft_test_snapshot；`proposal_generation` 用于 Plan/Group 的模型生成阶段，不与正式执行混为一个 WorkItem。

## 6. WorkItem 与 Runner

### `work_item`

`id PK`、`run_id FK`、`session_id FK`、`user_id`、`mode`、`owner_agent_id FK`、`agent_version_id FK`、`parent_work_item_id FK NULL`、`plan_node_id FK NULL`、`group_formation_proposal_id FK NULL`、`group_formation_work_item_id FK NULL`、`group_run_id FK NULL`、`group_node_id FK NULL`、`goal TEXT`、`status queued/running/waiting_user/waiting_agent/waiting_tool/waiting_resource/succeeded/failed/timed_out/cancelled/abandoned/blocked`、`active_runner_id FK NULL`、`execution_epoch BIGINT`、`attempt_no INT`、`result_summary TEXT`、`result_data JSON`、`error_code`、`error_detail TEXT`、`retryable BOOL`、`version`、时间。使用 `(run_id,user_id)` 复合 FK 指向 Run；Group 模式行使用 `(group_run_id,group_node_id)` 复合 FK 指向 `group_run_agent_version`，Chat/Plan 两列都为 NULL；索引 `(run_id,status)`、`(group_formation_proposal_id,status)`、`(group_run_id,group_node_id,status)`、`(status,updated_at)`。

MySQL 不支持通用 partial unique，唯一 active Runner 由锁定 WorkItem 后更新 `active_runner_id` 保证；Runner 另有唯一 `(work_item_id,execution_epoch)`。

### `runner`

`id PK`、`work_item_id FK`、`run_id FK`、`session_id FK`、`agent_id FK`、`agent_version_id FK`、`execution_epoch`、`status queued/running/succeeded/failed/cancelled/lost`、`lease_owner NULL`、`lease_token CHAR(36) NULL`、`lease_expires_at NULL`、`heartbeat_at NULL`、`principal_snapshot JSON`、`assembly_snapshot JSON`、`prompt_manifest JSON`、`input_snapshot JSON`、`result_message_id FK NULL`、`error_code/detail`、`version`、时间。唯一 `(work_item_id,execution_epoch)`；索引 `(status,lease_expires_at)`、`(session_id,created_at)`。

`runner_skill_snapshot(runner_id,skill_id,skill_version_id,checksum,path_uri,sort_order)`、`runner_tool_snapshot(runner_id,platform_tool_id,implementation_version,config_version,sort_order)` 与 `runner_policy_snapshot(runner_id,policy_definition_id,parameter_version,policy_type)` 关系化保存解释所需版本；完整 Prompt 文本不进入这些关系表。

## 7. Invocation、等待与确认

### `tool_invocation`

`id PK`、`runner_id/work_item_id/run_id/session_id FK`、`platform_tool_id FK`、`tool_key`、`execution_mode`、`status created/waiting_confirmation/waiting_resource/queued/running/succeeded/failed/rejected/cancel_requested/cancelled/outcome_unknown`、`arguments JSON`、`arguments_hash`、`business_idempotency_key`、`principal_hash`、`result_data JSON`、`error_code/detail`、`attempt_count`、`version`、时间。唯一 `(work_item_id,business_idempotency_key)`；索引 `(status,updated_at)`、`(platform_tool_id,status)`。Confirmation 通过 `tool_confirmation.tool_invocation_id UNIQUE` 反向一对一查询；WorkspaceLockRequest 通过 `tool_invocation_id` 反向一对多查询，避免循环 FK并允许异步 Tool 分阶段重新取锁。

### `tool_attempt`

`id PK`、`tool_invocation_id FK`、`attempt_no`、`status queued/running/succeeded/failed/timed_out/cancelled/outcome_unknown`、`worker_id`、`lease_token`、`provider_request_id NULL`、`request_snapshot JSON`、`response_snapshot JSON`、`error_class/code/detail`、`retryable`、时间。唯一 `(tool_invocation_id,attempt_no)`、`UNIQUE(tool_invocation_id,provider_request_id)`（非空时生效）。

### `agent_invocation`

`id PK`、`caller_work_item_id FK`、`caller_runner_id FK`、`target_work_item_id FK UNIQUE NULL`、`target_agent_id FK`、`target_agent_version_id FK`、`mode`、`request_text`、`context_snapshot JSON`、`status created/queued/running/succeeded/failed/cancelled/timed_out/rejected`、`batch_key NULL`、`result_summary/data`、`error_code/detail`、`idempotency_key`、`version`、时间。唯一 `(caller_work_item_id,idempotency_key)`；索引 `(caller_work_item_id,batch_key,status)`、`(target_work_item_id)`。

### `clarification`

`id PK`、`work_item_id/run_id/session_id FK`、`group_session_id/group_node_id NULL`、`asked_by_agent_id FK`、`target_user_id`、`question TEXT`、`status open/answered/cancelled`、`answer_message_id FK NULL`、`answer_text TEXT NULL`、`answered_by_user_id NULL`、`idempotency_key`、`version`、时间。唯一 `(work_item_id,idempotency_key)`；索引 `(target_user_id,status,created_at)`。

### `tool_confirmation`

`id PK`、`tool_invocation_id FK UNIQUE`、`user_id`、`tool_key`、`arguments_hash`、`principal_hash`、`status pending/approved/rejected/cancelled`、`decision_by_user_id NULL`、`decision_reason NULL`、`policy_version`、`version`、`decided_at`、时间。既有 `pending` 不因策略放宽自动改变。

## 8. Plan 图

### `plan_execution`

`id PK`、`run_id FK UNIQUE`、`session_id/user_id`、`owner_agent_id FK`、`owner_agent_version_id FK NULL`、`predecessor_plan_id FK NULL`、`goal TEXT`、`deliverable TEXT`、`definition_status draft/awaiting_confirmation/frozen/cancelled`、`graph_revision INT DEFAULT 1`、`confirmed_at`、`frozen_hash NULL`、`version`、时间。Plan 不重复保存执行 lifecycle/outcome；公开 lifecycle/outcome 始终读取关联 Run。Draft 编辑递增 graph_revision；确认后 `definition_status=frozen`，定义列与图行不可更新。

### `plan_work_item_node`

`id PK`、`plan_execution_id FK`、`node_key`、`title`、`goal TEXT`、`acceptance_criteria TEXT`、`assigned_agent_id FK`、`agent_version_id FK NULL`、`work_item_id FK UNIQUE NULL`、`sort_order`、`definition_version`。唯一 `(plan_execution_id,node_key)`、`(plan_execution_id,sort_order)`。

### `plan_work_item_dependency`

`plan_execution_id FK`、`predecessor_node_id FK`、`successor_node_id FK`、`dependency_type success/settled`，PK 三个 ID，禁止自环；确认时执行 DAG 无环校验。`plan_declared_agent(plan_execution_id,agent_id,agent_version_id)` 固定参与集合。

## 9. Group

### `GroupFormationProposal` 与 revision

`group_formation_proposal`：`id PK`、`parent_session_id FK`、`group_session_id FK UNIQUE`、`user_id`、`entry_agent_id FK`、`generation_run_id FK UNIQUE`、`status generating/awaiting_confirmation/confirmed/rejected/cancelled/failed`、`current_revision_id FK NULL`、`current_revision_no INT`、`version`、`decided_at`、时间。使用 `(parent_session_id,user_id)` 复合 FK；`(current_revision_id,id)` 复合 FK 指向 revision 的 `(id,proposal_id)`，保证 current revision 属于本 Proposal。索引 `(parent_session_id,status,created_at)`。Proposal generation 的 Run 使用 `mode=group,execution_kind=proposal_generation`，生成 WorkItem 通过 `group_formation_proposal_id` 关联，Runner 经 WorkItem 关联；GroupSession 此时已存在但为 forming，不能接受群消息或执行。

每次模型生成或用户编辑都创建不可变 `group_formation_revision`：`id PK`、`proposal_id FK`、`revision_no`、`source generated/user_edit`、`derived_from_revision_id FK NULL`、`goal TEXT`、`bootstrap_context MEDIUMTEXT`、`content_hash`、`edited_by_type user/agent`、`edited_by_id`、时间，唯一 `(proposal_id,revision_no)`、`(id,proposal_id)`。Proposal 的 `current_revision_id/current_revision_no` 在 `If-Match + expected_revision_no` CAS 成功后一起前移；旧 revision 保留编辑历史。首次 revision 前 current_revision_id 允许 NULL。

核心图按 revision 关系化：

- `group_formation_participant(id PK,revision_id FK,participant_key,agent_id FK,is_entry,initial_responsibility,sort_order)`，唯一 `(revision_id,participant_key)`、`(revision_id,agent_id)`、`(id,revision_id)`；生成列 `entry_revision_id=IF(is_entry,revision_id,NULL)` 加 `UNIQUE(entry_revision_id)`，确保每 revision 最多一个入口，保存/确认事务另保证至少一个。
- `group_formation_work_item(id PK,revision_id FK,work_item_key,participant_id FK,title,goal,acceptance_criteria,sort_order)`，唯一 `(revision_id,work_item_key)`；participant 必须属于同 revision，由复合 FK `(participant_id,revision_id)` 保证。
- `group_formation_suggested_edge(id PK,revision_id FK,source_participant_id FK,target_participant_id FK,label,rationale,sort_order)`，唯一 `(revision_id,source_participant_id,target_participant_id)`，禁止自环；两端使用复合 FK 保证同 revision。
- `group_formation_bootstrap_source(revision_id,source_type message/execution_event/artifact,source_id,sort_order,shared_excerpt_hash)`，PK `(revision_id,source_type,source_id)`；服务层校验来源属于同一 UserSession/userId。核心图不得放入 Proposal JSON。

`rejected/cancelled/failed` 为 Proposal 终态，并把对应 forming GroupSession 关闭且不创建节点/GroupRun。确认事务锁 `UserSession → generation Run → Proposal → forming GroupSession → current Revision`，重新校验 revision、参与者、Agent 访问和 Sandbox 兼容后，原子打开下述 GroupSession并把 Proposal 标 confirmed。

### `group_session` 与冻结 Formation

`group_session`：`id PK`、`parent_session_id FK`、`user_id`、`frozen_formation_revision_id FK UNIQUE NULL`、`entry_agent_id FK`、`status forming/open/closed/archived`、`goal TEXT NULL`、`bootstrap_context MEDIUMTEXT NULL`、`confirmed_at`、`closed_at`、`version`、时间。Proposal 以唯一 `group_session_id` 反向关联，避免循环 FK。GroupSession 与 Proposal 同事务创建为 forming；确认后填充冻结字段并转 open，拒绝/取消/失败则转 closed。forming 不可接收群消息或 GroupRun。索引 `(parent_session_id,status,created_at)`。

`group_bootstrap_source(group_session_id,source_type,source_id,sort_order,shared_excerpt_hash)` 是确认 revision 来源行的冻结副本，PK `(group_session_id,source_type,source_id)`。它与 `bootstrap_context` 一起构成可执行共享边界，不依赖可变 Chat 历史。

### `group_agent_node`

`id PK`、`group_session_id FK`、`agent_id FK`、`source_participant_id FK UNIQUE`、`is_entry BOOL`、`initial_responsibility TEXT`、`availability available/unavailable`、`unavailable_reason NULL`、`created_at`。唯一 `(group_session_id,agent_id)`、`(id,group_session_id)`；生成列 `entry_group_id=IF(is_entry,group_session_id,NULL)` 加 `UNIQUE(entry_group_id)` 保证最多一个入口，确认事务校验至少一个。节点集合确认后禁止 INSERT/DELETE/agent_id UPDATE；availability 只是当前治理投影，不删除节点。

### 建议边、实际边与初始 WorkItem

确认后的建议边保存在 `group_initial_suggested_edge(group_session_id,source_node_id,target_node_id,label,rationale,source_edge_id)`，PK 三个 ID，两端用 `(node_id,group_session_id)` 复合 FK；用于展示确认时的协作意图，不授予额外权限，也不表示已经发生调用。

`group_collaboration_edge` 只记录执行中实际发生的 `invoke_agent`：`id PK`、`group_session_id FK`、`source_node_id FK`、`target_node_id FK`、`first_invocation_id FK UNIQUE`、`created_in_group_run_id FK`、时间；唯一 `(group_session_id,source_node_id,target_node_id)`。两端必须属于同 Group，由复合 FK 保证。实际边只在首个成功创建的 AgentInvocation 事务中插入，不能由建议边预先伪造。

确认事务创建首个 GroupRun/Run，并把每条 `group_formation_work_item` 复制为正式 WorkItem，记录 `group_formation_work_item_id` 与对应 `group_node_id`；只为有初始 WorkItem 的节点创建 Runner。用户确认的初始工作是启动授权，不需要伪造 AgentInvocation。后续节点间工作仍必须通过 `invoke_agent`。

### `group_run`

`group_run`：`id PK`、`run_id FK UNIQUE`、`group_session_id FK`、`owner_node_id FK`、`goal TEXT`、`version`、时间。唯一 `(id,group_session_id)`，并以 `(owner_node_id,group_session_id)` 复合 FK 保证 owner 属于该固定 Group。它不重复保存 status/outcome；公共 lifecycle/outcome 始终来自关联 Run。索引 `(group_session_id,created_at)`。

`group_run_agent_version(group_run_id,group_session_id,group_node_id,agent_version_id NULL,participation_status available/unavailable,unavailable_reason NULL)` PK `(group_run_id,group_node_id)`；`(group_run_id,group_session_id)` 和 `(group_node_id,group_session_id)` 分别复合 FK 到 GroupRun 与 GroupAgentNode，保证不能混入图外节点。CHECK 要求 available 时 version 非 NULL 且 reason 为 NULL、unavailable 时 version 为 NULL 且 reason 非 NULL。每个固定 GroupAgentNode 都必须恰有一行，由创建事务锁定 GroupSession、全部节点及相关 Agent 后逐一插入并在提交前计数校验；PK 保证同节点没有两份映射，完整覆盖由锁定事务保证。

创建新 GroupRun 时，当前可用节点固定其最新已发布 AgentVersion；不可用节点保留并固定为 unavailable。`owner_node_id` 对应映射必须为 available，未显式指定 owner 时使用入口节点；入口不可用则整个创建事务失败，但非 owner 不可用不阻塞创建。映射提交后不可 UPDATE/DELETE，Agent 后续停用或重新启用都只影响未来 GroupRun。服务层在创建 WorkItem、定向 Message 和 AgentInvocation 时校验目标映射为 available；WorkItem 的复合 FK 保证执行目标属于同轮，Message/Invocation 的可选目标在创建事务中校验并通过 WorkItem 关系落地。AgentVersion 必须属于节点 Agent、owner 必须 available、映射必须完整等跨表断言由锁定事务保证，不能只靠普通 FK 表达。

`group_run_reference(group_run_id,referenced_group_run_id)` 保存历史引用。Group 发布记录用 `group_artifact(group_session_id,artifact_id,published_by_node_id,message_id)`。

## 10. Sandbox、Workspace 与 Artifact

### `session_sandbox` / `workspace`

`session_sandbox`：`id PK`、`session_id FK UNIQUE`、`sandbox_profile_id FK`、`profile_config_version`、`status provisioning/ready/failed/cleaned`、`root_uri`、`version`、时间。

`workspace`：`id PK`、`session_sandbox_id FK UNIQUE`、`session_id FK UNIQUE`、`root_uri`、`status active/cleaning/cleaned/failed`、`retention_policy_key session_lifetime`、`version`、时间。绝对路径只在 Sandbox 实现内部由 `root_uri` 解析。

### `workspace_lock`

每个 Workspace 一行：`workspace_id PK`、`writer_request_id NULL`、`writer_fencing_token BIGINT`、`writer_lease_expires_at`、`next_ticket BIGINT`、`version`。读锁持有者在 `workspace_read_lease(id,workspace_id,request_id,ticket,lease_token,expires_at)`；写请求在 `workspace_lock_request(id,workspace_id,tool_invocation_id,mode read/write,status queued/granted/released/expired,ticket,fencing_token,...)`。索引 `(workspace_id,status,ticket)` 实现公平顺序。

### `artifact`

`id PK`、`user_id`、`session_id FK`、`workspace_id FK`、`relative_path`、`name`、`mime_type`、`size_bytes`、`checksum`、`created_by_agent_id FK`、`work_item_id/runner_id/tool_invocation_id FK NULL`、`parent_artifact_id FK NULL`、`status published/unavailable/deleted`、`version_no`、时间。唯一 `(session_id,relative_path,version_no)`、`UNIQUE(parent_artifact_id,version_no)`；索引 `(session_id,status,created_at)`。Artifact 元数据保留随 Session；清理文件只改 `unavailable`，不删审计行。

## 11. Channel 与 NotificationDelivery

`channel_account`：`id PK`、`plugin_key`、`owner_account_id FK`、`external_account_key`、`credential_ref`、`status active/disabled/revoked`、`config JSON`、`version`、时间；唯一 `(plugin_key,external_account_key)`。`agent_channel_binding(channel_account_id,external_route_key,agent_id,status,version)` 把入站入口绑定到 Agent；`channel_identity_binding(channel_account_id,external_user_id_hash,user_id,verified_at)` 唯一前两列；`channel_conversation_binding(channel_account_id,external_conversation_id,session_id,user_id,status)` 唯一 `(channel_account_id,external_conversation_id)` 与 `session_id`。外部 ID 可按部署加密，索引值使用稳定 hash。

### `notification_delivery`

`id PK`、`session_id FK`、`user_id`、`run_id FK NULL`、`group_run_id FK NULL`、`triggering_event_id FK`、`event_type clarification_required/graph_confirmation_required/tool_confirmation_required/run_completed/run_failed/run_cancelled`、`template_key`、`template_version`、`channel_account_id FK`、`external_conversation_id`、`status queued/delivering/retry_wait/succeeded/failed/cancelled/outcome_unknown`、`dedupe_key VARCHAR(191)`、`provider_idempotency_key`、`provider_message_key NULL`、`attempt_count`、`last_error_code/detail`、`next_attempt_at`、`version`、`delivered_at/cancelled_at`、时间。唯一 `(session_id,dedupe_key)`、`(channel_account_id,provider_idempotency_key)`、`(channel_account_id,external_conversation_id,provider_message_key)`（provider message key 非空时生效）；索引 `(status,next_attempt_at,created_at)`、`(session_id,created_at)`、`(run_id,status)`。

`notification_delivery_attempt`：`id PK`、`delivery_id FK`、`attempt_no`、`status queued/delivering/succeeded/failed/timed_out/outcome_unknown`、`claim_token`、`provider_request_key NULL`、`provider_message_key NULL`、`request_metadata JSON`、`response_metadata JSON`、`error_code/detail`、`retryable`、时间；唯一 `(delivery_id,attempt_no)`、`(delivery_id,provider_request_key)`（非空）。正文由 `template_key/version + triggering_event_id` 在发送前生成安全渠道消息，不把完整用户内容复制进 Outbox。

只有 `origin_type=channel` 且存在 active `channel_conversation_binding` 的 Session 才创建 NotificationDelivery；Web/API Session 不创建虚假 delivery 行。Delivery 的状态事件属于该 Session ExecutionEvent，但 Attempt 原始诊断为 private。投递记录至少随 Session 保留，Attempt 可在终态 90 天后按策略清理；provider message key 与最终状态保留用于去重和审计。

## 12. History、Memory、Event、Audit、Checkpoint、Outbox

### `history_summary`

`id PK`、`session_id FK`、`scope_type chat/group_agent`、`group_session_id/group_node_id NULL`、生成列 `scope_discriminator = IF(scope_type='chat','chat',CONCAT('group_node:',group_node_id)) STORED NOT NULL`、`through_message_id FK`、`through_event_sequence`、`summary MEDIUMTEXT`、`source_hash`、`status pending/ready/failed/superseded`、`model_id/parameter_version`、`version`、时间。唯一 `(session_id,scope_discriminator,through_event_sequence)`；CHECK 要求 chat 的 group 字段为 NULL、group_agent 的两字段非 NULL。避免 MySQL nullable UNIQUE 允许多个 NULL；只使用 `ready` 最新行。

### `MemoryHead`、`memory` 与 candidate

`memory_head` 保存逻辑事实槽：`id PK`、`user_id FK`、`scope user/user_agent`、`agent_id FK NULL`、`fact_key VARCHAR(255)`、生成列 `scope_discriminator = IF(scope='user','user',CONCAT('user_agent:',agent_id)) STORED NOT NULL`、`version`、`last_user_edited_at NULL`、时间。CHECK 要求 user scope 的 agent_id 为 NULL、user_agent 必须非 NULL；唯一 `(user_id,scope_discriminator,fact_key)`，因此不受 nullable agent_id 影响。

`memory` 是不可覆盖的事实版本：`id PK`、`head_id FK`、`user_id`、`agent_id FK NULL`、`scope user/user_agent/agent`、`fact_key`、`content TEXT`、`normalized_hash CHAR(64)`、`status active/superseded/deleted`、生成列 `active_head_id = IF(status='active',head_id,NULL)`、`source_kind model/user_edit/import`、`source_session_id FK NULL`、`created_by_runner_id FK NULL`、`policy_definition_id FK NULL`、`confidence DECIMAL`、`supersedes_memory_id FK NULL`、`version`、时间。`UNIQUE(active_head_id)` 保证每个 Head 最多一个 active 版本；查询索引 `(user_id,scope,agent_id,status,updated_at)`。当前未启用的 agent scope 不创建 Head，也不由首批 API 写入。

`memory_source_message(memory_id,message_id,source_role)` PK 前两列保存所有来源。`runner_memory_snapshot(runner_id,memory_id,content_hash,scope,rank,score)` 保存已召回版本；Memory 后续编辑/删除不改历史 Runner 快照。

异步提取先写 `memory_write_candidate`：`id PK`、`user_id`、`agent_id NULL`、`scope`、`fact_key`、`content`、`normalized_hash`、`source_session_id/runner_id/policy_definition_id`、`observed_head_version NULL`、`status pending/accepted/rejected/failed`、`rejection_code NULL`、`result_memory_id FK NULL`、`version`、时间；来源消息用 `memory_candidate_source_message`。Worker 锁定/创建 Head，重新验证来源和 head version：同内容可接受为来源补充；兼容内容创建新 active 并 supersede 旧版；冲突不确定或用户在 candidate 生成后编辑则 rejected，不覆盖用户值。

### `execution_event`

`id PK`、`session_id FK NOT NULL`、`run_id FK NULL`、`group_session_id/group_run_id NULL`、`sequence BIGINT`、`event_type`、`event_dedupe_key VARCHAR(191)`、`transition_key NULL`、`actor_type/id`、`entity_type/id`、`entity_version`、`visibility user/group/direct/private`、`private_owner_type/id NULL`、`summary`、`payload JSON`、`caused_by_event_id FK NULL`、`correlation_id`、时间。唯一 `(session_id,sequence)`、`(session_id,event_dedupe_key)`；索引 `(run_id,sequence)`、`(group_session_id,sequence)`、`(entity_type,entity_id,created_at)`、`(transition_key)`。`execution_event_recipient(event_id,recipient_type,recipient_id)` PK 三列并索引 `(recipient_type,recipient_id,event_id)`；direct 事件至少一个 recipient，private 事件必须有 owner，由 EventWriter 校验。表权限/trigger 拒绝 UPDATE，追加后不可修改，默认与业务记录同生命周期。幂等和投影见 [事件与可见性](events-audit-visibility.md)。

### `platform_audit_event`

`id PK`、`occurred_at`、`category identity/agent/registry/skill/channel/security/maintenance`、`action`、`actor_type account/api_key/service/anonymous`、`actor_id NULL`、`actor_user_id NULL`、`request_id/correlation_id NULL`、`resource_type/id`、`resource_version NULL`、`outcome succeeded/failed`、`severity info/warning/critical`、`summary`、`changes JSON`、`metadata JSON`、`source_ip_hash/user_agent_hash NULL`、`dedupe_key VARCHAR(191)`、`created_at`。`UNIQUE(dedupe_key)`；索引 `(resource_type,resource_id,occurred_at)`、`(actor_type,actor_id,occurred_at)`、`(category,action,occurred_at)`、`(outcome,severity,occurred_at)`。它无 session_id/sequence，不进入 Session SSE；changes 只存字段名与脱敏差异。表权限/trigger 拒绝普通 UPDATE，只有专用 retention procedure 可按已到期时间物理删除。成功治理事件至少保留 365 天，认证失败至少 180 天。

### `runner_checkpoint`

`id PK`、`runner_id FK`、`work_item_id FK`、`sequence_no INT`、`checkpoint_type`、`state_snapshot JSON`、`last_completed_step`、`safe_to_resume BOOL`、`created_at`。唯一 `(runner_id,sequence_no)`；索引 `(work_item_id,safe_to_resume,created_at DESC)`。WorkItem 终态 30 天后可清理，受审计保留策略覆盖时延长。

### `outbox_command`

`id PK`、`topic runtime.control/runtime.runner/runtime.tool/runtime.sandbox/runtime.delivery/runtime.maintenance`、`action`、`aggregate_type/id`、`partition_key`、`dedupe_key`、`payload_ref JSON`、`priority`、`available_at`、`status pending/claimed/retry_wait/completed/dead`、`attempt_count`、`dispatch_lease_owner/token/expires_at`、`last_error`、`published_at NULL`、`completed_at NULL`、时间。唯一 `(topic,dedupe_key)`；领取索引 `(topic,status,available_at,priority,created_at)`、回收索引 `(status,dispatch_lease_expires_at)`。dedupe_key 必须由不可清理的领域 transition/cause ID 生成；消费者先检查领域状态，因此 Completed 行清理后旧命令重现仍不能重复推进。Completed 保留 30 天（不得早于上游最大重投窗口），dead 至少 180 天；Session 命令关联 ExecutionEvent，平台命令关联 PlatformAuditEvent。

## 13. 保留与删除边界

- Published AgentVersion、Plan/Group 冻结定义、Invocation、ExecutionEvent、Artifact 元数据不级联物理删除。
- 用户删除 Session：先 `closed/archived`，异步清理 Workspace 文件并把 Artifact 标记 unavailable；领域记录按部署审计策略保留。
- 未被测试 Run 引用的 draft_test_snapshot、失败上传 SkillVersion 可在 30 天后清理；当前 draft 不按时间清理。
- Trace 独立保留（默认 30 天）；Token 临时流不保留；Checkpoint 默认终态后 30 天；Outbox 按上述窗口。
- FK 删除策略默认 `RESTRICT`；只有纯关联表、未发布草稿子项允许 `ON DELETE CASCADE`。

## 14. MySQL 约束实现清单

| 不变量 | 数据库直接保证 | 锁定事务/服务保证 |
| --- | --- | --- |
| 一个 Agent 最多一个 draft | generated `draft_agent_id` + UNIQUE | Agent 创建提交前必须回填 draft_version_id；一致性 job 检查非空且同 Agent |
| Published/test snapshot 不可变 | immutable_at seal 后 AgentVersion 与三张关系表 trigger 拒绝变更，FK RESTRICT | 发布服务只复制并 seal，不把 draft 原地转态；未 sealed 版本不可选择 |
| version_no 唯一递增 | `(agent_id,version_no)` UNIQUE | 锁 Agent 分配 next_version_no；无间隙不是业务要求 |
| History Chat 摘要唯一 | 非空 generated scope_discriminator UNIQUE | 保存前校验 scope/group 字段组合 |
| Memory 每逻辑键一个 active | Head 非空 discriminator UNIQUE；Memory generated active_head_id UNIQUE | 锁 Head，先 supersede/delete 旧 active，再插入新版；candidate 复核 head version |
| Group 每 revision/正式 Group 恰一入口 | generated entry key UNIQUE 保证最多一个 | revision 保存和确认事务计数必须等于 1 |
| Proposal 图同 revision | participant/work item/edge 复合 FK | DAG/业务合法性与访问权由保存、确认事务校验 |
| Proposal revision 不可改写 | revision 及四类子表 trigger 拒绝 UPDATE/DELETE | 编辑只 INSERT revision_no+1，并以 Proposal CAS 前移 current revision |
| Group 节点冻结 | confirmed Group 的 node trigger 拒绝 INSERT/DELETE/agent_id UPDATE | 确认事务一次性复制；availability 可更新但不改变集合 |
| Plan 定义冻结 | definition_status=frozen 后 Plan node/dependency trigger 拒绝写 | confirm 锁 Plan/Run 并计算 frozen_hash |
| Event 合法重复诊断 | 仅 `(session_id,event_dedupe_key)` UNIQUE | EventWriter 按 transition/cause 生成 key；entity_version 不唯一 |
| Event audience 完整 | recipient FK/PK 防重复与悬空 | EventWriter 在同事务保证 direct 至少一 recipient、private 必有 owner；读取统一投影二次授权 |
| Outbox 清理后不重复推进 | 领域唯一键、状态 CHECK、Runner epoch/Invocation key 仍保留 | handler 始终回读领域状态；dedupe key 来源 transition/cause，不只依赖 Outbox 行 |
| 冗余 user_id 一致 | 关键链路 `(id,user_id)` UNIQUE + 子表复合 FK | 多父表在固定锁顺序下逐一校验；每日一致性扫描只告警，不静默修复 |
| 循环引用创建 | 初始 FK 均 nullable、迁移后 ALTER 添加 FK | Agent、Artifact/Message 等只允许专用事务按“父→子→回填”创建；提交后 invariant scan 不允许悬空 |

MySQL CHECK 负责枚举和单行条件，不用 CHECK 假装保证跨行计数。Trigger 只保护不可变与冻结边界，不承载复杂业务授权；授权、可访问性和图语义仍由服务层在短事务中验证。

有意允许多 NULL 的 UNIQUE 仅包括：Tool/Notification provider request/message key（尚未取得 Provider 标识的多次本地记录）、Artifact `parent_artifact_id`（多个根 Artifact 合法，根版本由 `(session_id,relative_path,version_no)` 约束）以及尚未发布的 version pointer。凡 NULL 代表同一逻辑桶的场景（History Chat、Memory user scope、Group entry）一律使用非空 generated discriminator，不直接依赖 nullable UNIQUE。
