# 企业生产运行手册

本文给出 `enterprise_production_v1` 的发布、值守、故障处置和恢复步骤。架构与数值目标分别以[企业生产参考架构](../technical/production-reference-architecture.md)和[生产就绪要求](production-readiness.md)为准。环境尚未通过生产 Gate 时，本手册只能用于演练，不能作为已具备生产能力的证明。

## 1. 角色与权限

| 角色 | 职责 | 禁止事项 |
| --- | --- | --- |
| Release Owner | 批准 release manifest、开始/停止发布 | 单人修改生产数据或跳过 Gate |
| Incident Commander | 事件定级、协调、时间线和对外状态 | 同时充当所有修复执行者 |
| Database Operator | failover、PITR、备份验证 | 通过应用账号执行 DDL |
| Security Owner | 身份、Secret、Sandbox 和数据泄漏处置 | 使用普通 admin 读取用户正文 |
| Service Owner | API/Runtime/Tool/Storage 指标与 Runbook | 手改终态领域行“修复”状态 |
| Evidence Recorder | 保存命令摘要、时间、结果和 artifact ID | 记录 Secret、Token、Prompt 或用户全文 |

生产写权限使用临时、限时、可审计凭证。break-glass 必须关联事件/工单、双人批准、最长 60 分钟并在结束后立即撤销。

### 事件级别与响应时限

| 级别 | 判定 | 首次确认 / IC 就位 | 状态更新 |
| --- | --- | --- | --- |
| P0 | 已确认或高度可信的跨账号/跨 Tenant 访问、不可逆数据丢失、Sandbox escape/Secret 外泄、审计篡改、重复高影响副作用或全域不可用 | 5 分钟 / 10 分钟 | 至少每 30 分钟 |
| P1 | 单项关键能力大面积不可用、RPO/RTO 即将或已经违约、Artifact/身份完整性事件、持续 fast burn，但尚无 P0 证据 | 15 分钟 / 30 分钟 | 至少每 60 分钟 |
| P2 | 有界降级且有安全替代路径、未破坏数据/授权/SLO 月度预算 | 4 小时 / Service Owner 当班接管 | 每工作日或状态变化 |
| P3 | 低风险缺陷、告警噪声或文档/可观测性改进 | 1 个工作日 | 按工单里程碑 |

任何人都可升级事件，只有 Incident Commander 在证据支持下可以降级。P0/P1 立即冻结发布，建立 UTC 时间线、影响范围和决策日志；是否通知客户/监管方由 Security、法务和业务 Owner 按合同决定，技术人员不得自行隐藏或对外定性。

## 2. 每次发布必须准备的 Manifest

Release Owner 在发布前固定：

```text
release_id
git_sha (clean, protected ref)
container_image_digests by component
schema_head / migration checksum
openapi_sha256
configuration_revision
deployment_profile=enterprise_production_v1
identity_profile=enterprise_oidc_v1
tenant_id (exactly one active enterprise tenant)
identity_provider_revision + workload_policy_revision
capacity_policy_revision + retention_policy_revision
artifact_bucket_policy_digest + ledger_seal_bucket_policy_digest
kms_key_versions for secrets/artifacts/ledger seals
sandbox_profile_version_ids + image digests
agent_version_ids + content hashes
skill/model/policy version ids + hashes
uat/load/security/restore/failover evidence ids
approvers and approval timestamps
rollback image set and last compatible schema
```

任一值缺失、使用 mutable image tag、OpenAPI/Schema/状态枚举不一致、证据来自不同 SHA，发布立即停止。

## 3. 发布前检查

按顺序执行并记录结果：

1. 验证 Git tree clean、release ref 受保护、镜像签名和 SBOM；
2. 在空库执行全部 migration，再从上一生产 Schema 执行升级；两条路径结果必须同一 schema hash；
3. 解析 OpenAPI，检查 `$ref`、operationId、path parameter、请求/响应和状态枚举；
4. 验证 OIDC issuer/JWKS/audience/scope、OBO client 和 service principal 的正负用例；验证 PKCE/state/nonce、callback 重放、Session 轮换/撤销、CSRF/CORS；确认 raw Header 在生产返回 `invalid_identity_profile`；
5. 验证 Secret Manager/KMS、MySQL writer/replica、Workspace、Artifact bucket、Redis 和 Telemetry 健康；
6. 验证 Sandbox 使用要求的 runtime class/image digest，网络默认拒绝，metadata/私网/宿主路径不可达；
7. 记录最新成功备份、PITR 可用点、跨区域对象复制 lag；不满足 RPO 时停止；
8. 检查 Outbox oldest age/dead、Runner lost、outcome_unknown、Artifact mismatch 和安全告警为可解释基线；确认 Event/Audit 最新 seal 小于 24 小时且全链、KMS 签名、不可覆盖对象验证通过；
9. 确认值班人员、Incident Commander、Database/Security Operator 和回滚窗口在线；
10. 在生产等价 staging 完成 smoke、并发、故障注入和一次恢复演练。

## 4. 发布步骤

固定顺序：

1. 将发布状态设为 `in_progress`，冻结无关 Schema/身份/Sandbox 变更；
2. 执行只包含 expand 的兼容 migration；禁止 drop/rename/收紧未完成 backfill 的列；
3. 部署一个 API canary，验证 readiness manifest、认证、只读查询、幂等 Run 创建和 SSE；
4. 部署一个 Control/Runner canary，用专用 AgentVersion 验证普通 final、Tool Call、等待/恢复和旧 lease fencing；
5. 部署 Tool/Delivery/Sandbox canary，验证对象 checksum、Workspace 权限和网络拒绝；
6. API/Worker 分批扩大到 25% → 50% → 100%，每档至少观察 15 分钟和足够请求样本；
7. 发布前端，验证旧前端与新 API、新前端与当前 API 的兼容窗口；
8. 保持 contract migration 禁止状态，持续观察至少 60 分钟；
9. 满足完整兼容窗口和 backfill 校验后，在后续独立发布执行 contract migration；
10. 将 manifest、dashboard snapshot、事件时间线和验证结果归档，发布状态改为 `completed`。

Canary 期间若出现 P0/P1、99.90% SLO fast burn、跨账号访问、重复副作用、未知 Schema、fencing reject 异常上升或 Artifact checksum mismatch，立即停止扩大并进入回滚/事件流程。

## 5. 应用回滚

1. 停止 rollout，保留现场指标、Trace ID、Event/Attempt/lease 状态；
2. 确认目标旧镜像与当前 Schema 在 manifest 中标记兼容；不兼容时不得强行回滚；
3. 将 API/Worker 各 lane 回滚到固定 digest，不使用 floating tag；
4. 不回滚已提交领域数据，不执行 destructive Schema downgrade；
5. Reconciler 处理 canary 遗留 Runner/Outbox/Artifact publishing，旧 Worker 必须重新取得 lease；
6. 验证新 Run、既有等待恢复、SSE、Tool 幂等和 Artifact 下载；
7. 记录根因、影响、未确认副作用和后续处置。只有指标回到基线并经 Incident Commander 同意才解除冻结。

## 6. MySQL writer 故障切换

1. API/Worker 进入 `dependency_unavailable`/停止新副作用模式；不要让旧 writer 与候选 writer 同时可写；
2. 记录最后已确认 writer position、replica lag、最近备份/PITR 点；
3. 按数据库平台流程 fence 旧 writer，再提升最完整的跨区 replica；
4. 更新 writer endpoint，先只恢复只读健康检查；
5. 执行领域一致性查询：Run/GroupRun 一对一、active Runner 指针/epoch、Event sequence/hash chain、Audit chain/最新 seal、Outbox cause/dedupe、Artifact published/checksum；
6. 启动一个 Control/Runner canary，让过期 lease 被标 lost 并创建新 epoch；验证旧 token 写入被拒绝；
7. 逐 lane 恢复写流量和副作用；任何 outcome_unknown 进入人工/远端对账，禁止批量重试；
8. 记录实际 RPO/RTO、丢失或重放窗口，并更新演练证据。

## 7. 全区域灾难恢复

1. Incident Commander 宣告 region disaster，Edge 保持旧区域写入口关闭；
2. 在 DR 区按固定 Terraform/GitOps revision 建立网络、Kubernetes 和私有 Endpoint；
3. 从跨区域备份/PITR 恢复 MySQL 到选定时间点，记录恢复 position；
4. 恢复/挂载 Workspace snapshot，确认租户/account/session 路径与 generation；
5. 等待 Artifact/Registry 对象复制到一致点，执行 DB metadata ↔ object checksum 对账；
6. 恢复 Secret reference 和工作负载身份，不复制导出的明文 Secret；
7. 部署固定 release digest，readiness 暂不接收业务流量；
8. 执行身份、Schema、Run/Outbox/lease、Workspace、Artifact、Event/Audit chain + KMS seal 和 SSE 一致性检查；
9. 所有旧 running lease 视为失效，由 Reconciler 创建新 epoch；外部 Attempt 先查 provider operation，不盲重放；
10. 先开放内部 canary，再切换 Edge；确认旧区域继续 fenced，避免双主；
11. 记录实际数据截止点、RPO、RTO、未知副作用和用户影响。

## 8. Outbox/Runner 积压

### 告警条件

- runtime.control oldest age > 30 秒；
- runtime.runner/tool oldest age > 2 分钟；
- dead command > 0；
- lost Runner 或 fencing reject 超出历史基线；
- waiting action resume p99 > 15 秒。

### 处置

1. 按 topic/command_type/error_code/partition_key 分组，不先全量重投；
2. 检查 MySQL lock/connection、Provider/Tool quota、Worker capacity 和 claim lease；
3. 只通过领域命令为 dead item 创建新的 dedupe command，禁止手改 status=pending；
4. Tool side_effect=non_idempotent 或 Attempt=outcome_unknown 时先对账；
5. 扩容只增加对应资源 lane，不按 Agent/Chat/Plan/Group 建临时队列；
6. backlog 清空后验证每个 WorkItem 最多一个 active Runner、终态未复活、无重复外部 operation。

## 9. Redis 故障

1. 确认 MySQL、API 和 Worker 仍健康；Run 不应停止；
2. 标记 provisional stream degraded，客户端停止依赖 delta 气泡并从可靠 Event/Message 刷新；
3. 恢复 Redis HA 或创建空集群；不从 Redis 反向写回业务状态；
4. 重建限流/短 TTL 数据，客户端按 event_id/sequence 去重；
5. 若业务状态因 Redis 丢失而变化，按 P0 数据权威违规处理。

## 10. Channel Provider / Delivery 事件（启用 Channel 时）

1. 暂停受影响 ChannelAccount/Provider 的新 Delivery claim，不取消已提交 Run；
2. 按 provider、account、error code、attempt 和 provider idempotency key 分组，核对是未发送、明确失败还是可能已送达；
3. Provider 支持幂等/状态查询时，只用原 `provider_idempotency_key` 查询或安全重试；不支持且结果不明时结算 `outcome_unknown`；
4. 通过站内可靠 Event/API 保留原结果可读，禁止转投其他 Channel 或改写 Run 终态；
5. 恢复后限速开放 pending Delivery，监控 duplicate provider message、oldest age、dead/outcome_unknown，并把对账证据绑定 release/incident。

## 11. Sandbox 安全事件

1. 立即停止受影响 Sandbox Profile 的新 Session/Runner 准入，隔离节点池和 egress；
2. 撤销该 workload identity、临时 Tool/Provider credential 和签名 URL；
3. 保留 VM/image digest、网络流、ToolInvocation、Workspace generation 和审计证据，不保存用户 Secret 明文；
4. 已运行 Tool 按实际 Attempt 结算；未知副作用进入 outcome_unknown；
5. 扫描同 image/profile revision 的全部 Session，轮换可能暴露的 Secret；
6. 修复后发布新的 SandboxProfileVersion/image digest，旧 AgentVersion 不自动漂移；Builder 必须重新测试/发布或由治理策略明确阻止旧版本运行；
7. 完成 escape、egress、路径穿越、资源耗尽和跨 Session 回归后才能重新启用。

## 12. 身份/JWKS 事件

- JWKS 刷新失败但缓存 key 未过安全 TTL：继续验证已知 kid，告警并拒绝未知 kid；
- issuer/audience 配置错误：readiness not_ready，禁止 fallback 到 raw Header；
- client/subject 泄漏：撤销 IdP credential/external identity，阻止新请求，评估已持久 Run 并按安全策略取消；
- Browser Session 疑似盗用或 fixation：撤销该账号全部 active BrowserAuthSession、递增 credential revision、保留 token hash/来源/时间审计并要求重新 OIDC 登录；不得仅清理浏览器 Cookie；
- 跨账号访问迹象：P0，冻结相关 tenant 写入，保留 PlatformAuditEvent、AuthContext ID 和资源访问记录；
- service principal 尝试决定自然人 Approval：必须 403，若成功视为 P0 授权不变量破坏。

## 13. Secret 轮换

1. 在 Secret Manager 创建新版本，不先删除旧版本；
2. 更新 credential revision，canary 验证 IdP/Provider/Tool/DB 连接；
3. 滚动刷新使用方，让新请求使用新版本；已运行 Attempt 保留其 revision；
4. 观察认证失败和 outcome_unknown；
5. 撤销旧版本，验证旧 credential 负向测试；
6. 归档轮换时间、范围、验证和异常，不记录 Secret 值。

## 14. 审计账本完整性事件

Event/Audit 链不匹配、KMS 签名失败、seal 对象缺失/被覆盖，或最新成功 seal 超过 24 小时，均由 Security Owner 接管：

1. 立即停止受影响 Tenant 的 Registry、身份、授权、Retention、break-glass 等管理写入；若 ExecutionEvent 链受影响，同时停止该 Session 的新执行。其他 Tenant/Session 只有在证明链和存储隔离后才能继续；
2. 保全 writer、只读副本、binlog/PITR、最近备份、KMS key version、外部 seal 对象、访问日志和相关发布 Manifest；不得运行修复脚本、删除行或重新生成历史 hash；
3. 从最近一个签名和对象锁均验证通过的 seal 开始，使用对应 `hash_schema_version` 重放校验，记录首个不匹配 sequence、数据库副本差异和所有访问主体；
4. 撤销可疑数据库/运维凭证并隔离受影响 writer；需要 failover 时只切到已证明链头不晚于可信 seal 且满足 RPO 的副本，否则从隔离 PITR 恢复；
5. 恢复服务前必须完成链、签名、授权边界和领域一致性验证。可信 seal 之后的事实即使可从业务系统重建，也只能作为新的补偿/恢复记录追加，不能冒充原历史；
6. 事件报告记录影响范围、最后可信 sequence/hash、数据缺口、RPO/RTO、取证位置、通知决定和整改 Owner。无法证明完整性的时间段必须明确标记为不可信。

## 15. Artifact 对账

Reconciler 周期扫描：

- DB=publishing 且临时对象完整：按 checksum 幂等完成；
- DB=publishing 且对象缺失/损坏：标 unavailable(publish_failed)，清理后转 deleted，不伪造 published metadata；
- DB=published 但最终对象缺失/不匹配：转 unavailable(storage_fault|checksum_mismatch)，P1 数据完整性事件，禁止返回其他字节；
- 从受信副本找回原 checksum：由 Storage + Security Owner 双重确认，create-if-absent 恢复相同 key，读回复核、对象锁验证后 CAS 回 published 并写 artifact.restored；
- 无 DB 引用的临时对象：超过 24 小时后按审计 Retention 删除；
- 同 artifact_id 出现不同 checksum：P0 不可变性破坏，冻结 Artifact 写入。

任何对账都不能把另一对象复制覆盖已发布 key。

## 16. 演练频率

| 演练 | 最低频率 |
| --- | --- |
| MySQL failover + lease/fencing | 每季度及数据库重大升级前 |
| 隔离 PITR + 领域一致性 | 每季度 |
| 全区域 DR | 每半年 |
| Secret/Signing key 轮换 | 每季度或供应商要求更短周期 |
| Sandbox escape/egress/路径穿越 | 每次 runtime/image 大版本及每季度回归 |
| OIDC/OBO 负向与 confused deputy | 每次身份配置变更及每季度 |
| Outbox backlog/dead、Provider/Tool unknown | 每季度 |
| Event/Audit hash chain、KMS seal 与篡改取证 | 每季度及 hash/KMS/存储策略变更后 |
| 滚动发布和应用回滚 | 每个大版本候选 |

演练报告必须绑定 release SHA、镜像、Schema、配置、开始/结束时间、期望、实际、RPO/RTO、异常和整改 Owner。未完成整改的失败演练不能作为生产 Gate 证据。
