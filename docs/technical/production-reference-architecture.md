# 企业生产参考架构

本文把 MyClaw 已确定的 Agent-native 领域架构映射为可实施、可扩缩和可演练的企业生产部署合同。产品语义仍以[权威模块](../README.md)为准；数据库、状态机和接口分别以[数据库契约](database-schema.md)、[状态机](state-machines.md)和 [OpenAPI](openapi-v1.yaml) 为准。

本文定义的是 `enterprise_production_v1` 默认参考实现，不表示当前代码或环境已经达到该 Profile。偏离本文的部署必须证明满足相同或更强的语义、安全、SLO 和恢复目标，并形成有 Owner、到期日和证据链接的 ADR；不能由实现人员临时选择不同语义。

## 1. 固定决策

| 主题 | `enterprise_production_v1` 决策 |
| --- | --- |
| 控制域 / Tenant | 一个部署对应一个 MyClaw 控制域，`enterprise_production_v1` 每个控制域只承载一个企业 Tenant；所有 tenant-owned 数据仍携带非空 `tenant_id` 并执行复合 FK/负向隔离测试，不把该 Profile 宣称为多客户 SaaS |
| 编排 | 单区域、三个可用区的 Kubernetes；无状态服务跨区部署 |
| 业务权威 | MySQL 8 / InnoDB 单写者；状态迁移不从 Redis、MQ、Trace 或对象存储反推 |
| 事务隔离 | `READ COMMITTED` + 显式短事务 + CAS + 固定锁顺序 |
| 调度 | Transactional Outbox 为可靠命令源；多个 Worker 使用 `SKIP LOCKED` 领取；MQ 只能作加速层 |
| 身份 | `identity_profile=enterprise_oidc_v1`；Web/API 使用 `oidc_user`，企业后端使用 `oidc_obo`，后台主体使用 `service_principal` |
| Agent 运行 | Agent 是逻辑身份/配置；Runner 由共享 Runtime Worker 按 WorkItem 按需承载，不为 Agent 分配常驻 IP 或进程 |
| Sandbox | 独立 Sandbox 服务在专用节点池启动 Kata/microVM 隔离实例；普通容器和 Local Sandbox 不允许进入生产 Profile |
| Workspace | 加密、多可用区 POSIX 存储；每 Session 一个逻辑根，File Tool/Sandbox Broker 强制 principal-aware 路径授权 |
| Artifact | 版本化对象存储；平台生成不可变 key、SHA-256 和 provenance；发布后禁止覆盖 |
| Knowledge vector | MySQL 保留授权/正文/revision 权威；Qdrant 集群只保存可重建 Embedding 投影，查询强制 Tenant + revision filter |
| 审计完整性 | ExecutionEvent 按 Session、PlatformAuditEvent 按 Tenant 建立版本化 hash chain；至少每日验链，KMS 签名 head 并保存不可覆盖 seal |
| 缓存/实时流 | Redis HA 只承载短 TTL、限流和 provisional token；可靠事件从 MySQL ExecutionEvent 读取 |
| 可观测性 | OpenTelemetry Collector HA + 指标/日志/Trace 后端；观测数据不参与授权和恢复 |
| Secret | 企业 Secret Manager/KMS；工作负载身份取短期凭证，不在数据库、镜像或 Kubernetes Secret 中保存长期明文 |

## 2. 物理拓扑

```text
Enterprise IdP
      │ OIDC / OBO
WAF / Rate Limit / L7 Load Balancer
      │
      ├── Web/API pods (>=3, across 3 AZ)
      │       ├── MySQL writer endpoint
      │       ├── Redis HA
      │       ├── Qdrant cluster
      │       └── Object/Workspace services
      │
      ├── Runtime control pods (>=3)
      ├── Runtime runner pods (>=3, autoscaled)
      ├── Knowledge index pods (>=2, autoscaled)
      ├── Tool/delivery pods (>=2 per enabled lane)
      └── Sandbox broker pods (>=3)
                  │ authenticated execution request
                  └── isolated sandbox nodes / Kata VMs

MySQL: one writer + cross-AZ failover replicas + PITR backups
Storage: multi-AZ Workspace + versioned immutable Artifact bucket + object-locked ledger seal bucket
Vectors: replicated Qdrant collection + scheduled snapshots; rebuild source remains MySQL + source objects
Telemetry: HA collectors → metrics/log/trace backends
DR: second-region infrastructure definition + replicated backups/artifacts
```

只有 Edge Load Balancer 可直接接收入站公网流量。Runtime、Tool、Sandbox、数据库、Redis、存储和 Telemetry 使用私网 Endpoint 与双向工作负载认证。管理接口通过独立管理入口、强认证和审计访问。

## 3. 服务边界与副本

| 服务 | 最低副本 | 本地状态 | 扩缩依据 | 故障语义 |
| --- | ---: | --- | --- | --- |
| Web/API | 3 | 无 | RPS、CPU、请求队列 | 重试只依赖 Idempotency-Key；不取消已创建 Run |
| Runtime Control | 3 | 无 | Outbox oldest age、等待恢复延迟 | 重复命令由 dedupe/CAS 收敛 |
| Runtime Runner | 3 | Runner lease | runnable 数、Provider 并发、Token 配额 | lease 过期后新 epoch 恢复；旧 Worker 被 fencing |
| Knowledge Index Worker | 2 | knowledge.index lease | queued age、chunk backlog、Embedding/Qdrant latency | deterministic point upsert；完整投影后才推进 current revision |
| Tool Worker | 每 lane 2 | ToolAttempt lease | lane backlog、外部并发限制 | outcome_unknown 禁止盲重试 |
| Delivery Worker | 2 | DeliveryAttempt lease | delivery backlog | 投递失败不改 Run 结果 |
| Sandbox Broker | 3 | 无 | provisioning latency、活跃实例 | 实例丢失按 Checkpoint/Attempt 恢复 |
| Maintenance | 2 个候选、1 个有效 lease | DB lease | 固定周期 | 不依赖单 Pod；抢锁后执行幂等巡检 |

Pod 使用跨区 anti-affinity、TopologySpreadConstraints 和 PodDisruptionBudget。API、Control、Runner、Tool、Sandbox Broker 在计划维护期间至少保留一个可用副本；滚动发布 `maxUnavailable=0`、`maxSurge>=1`。Sandbox 执行节点与控制面节点分离，不允许挂载集群管理凭证或宿主 Docker socket。

## 4. 数据与一致性

### MySQL

- 一个可写主节点，至少两个跨可用区故障副本；禁止多主写入同一控制域；
- 所有 Run/WorkItem/Runner/Invocation/Approval/Event/Outbox 状态迁移只写 writer；
- 只读副本仅承载允许陈旧的管理报表，授权、SSE 游标、恢复和调度查询不得读取副本；
- 数据库时间用于 lease/expiry 判定；应用节点时钟不决定执行所有权；
- Schema 使用 expand → migrate/backfill → contract，任何版本只能与前后一个 Schema 版本双向兼容；
- 迁移工具以数据库互斥锁保证单执行者，DDL 超时即停止发布，不自动重试破坏性变更；
- 每日全量备份、持续 binlog/PITR、跨区域复制；备份加密并使用 restore-only 权限季度演练。
- Maintenance 至少每日按数据库契约验证 Event/Audit hash chain，把链头交由独立 KMS 签名并写入开启版本锁/对象锁的不可覆盖存储；seal 超过 24 小时未生成或任一链/签名不匹配立即按安全事件处置，禁止改写数据库历史来“修复”校验结果。

### Outbox 与至少一次

领域状态、ExecutionEvent 和 OutboxCommand 在同一 MySQL 事务提交。生产默认直接由 Worker 使用 `SELECT ... FOR UPDATE SKIP LOCKED` 批量领取；当压测证明数据库轮询成为瓶颈时才增加 MQ Relay。即使启用 MQ：

1. Outbox 仍是命令可恢复事实；
2. Relay 只发布已提交命令 ID；
3. Consumer 必须回读数据库并取得领域 lease；
4. MQ ack 在数据库 acknowledge 后发生；
5. MQ 丢失、重复、重排不能改变业务结果。

### Redis

Redis 使用跨区复制和自动 failover，但其全部内容都必须可重建。Redis 不保存 Run status、审批结果、Agent membership、可靠 Event cursor 或 Tool outcome。Redis 故障时停止 provisional delta，API/SSE 回退 MySQL，后台执行继续。

### Qdrant

Qdrant 使用跨可用区副本和定期 snapshot，但不成为 Knowledge 正文、授权或 revision 权威。Collection 按冻结的维度与 ModelVersion content hash 分区，point payload 至少携带 `tenant_id`、`knowledge_base_id` 和 `index_revision_id` 并建立 payload index。API 查询先从 MySQL 得到允许访问的 ready revision，再以 Tenant/revision filter 查询 Qdrant，最后回到 MySQL 复核 current pointer 与 source checksum。Qdrant 全失时停止 semantic 查询和新索引发布；恢复 snapshot 或从 MySQL/source object 重建投影，不能把 point 缺失的 revision 标为 ready。

## 5. 身份与授权

生产禁用 `raw_user_id_v1` 和 Agent API Key + userId Header 组合。详细 Profile 见[身份与 Agent API](../modules/identity-api.md)。实现必须遵循：

- Edge 与 API 都校验 issuer/audience，但领域授权以 API 生成的不可变 AuthContext 为准；
- `(tenant_id, issuer, subject)` 映射 `external_identity → platform_account.id`；领域 FK 只使用 `account_id`；
- OBO 同时验证 workload client 和 delegated user token；用户 subject 不能来自 Header/body；
- 浏览器固定使用 Authorization Code + PKCE(S256)、一次性 state/nonce 和服务端 opaque Session；Cookie 为 Secure/HttpOnly/SameSite，状态命令强制同源 + Session-bound CSRF，登录/提权/撤销时轮换或撤销；
- 默认 `account_provisioning=preprovisioned`；未知受验证 subject 返回 `identity_mapping_required`，不按 email/display name 自动建号或换绑；
- service principal 使用独立 service account，不能回答自然人的 Approval；
- 所有资源查询同时绑定 tenant、account 和父资源；无读取权限返回 404 防枚举；
- AgentInvocation 继承原 Run 的 account，并携带当前 permission、credential 与 account authorization revision；目标 Agent 在每次新 dispatch 重新授权但不改变主体，账号停用或授权 revision 变化可阻止后续副作用；
- break-glass 使用独立角色、工单、双人审批、最长 60 分钟授权和全量 PlatformAuditEvent，不复用普通 admin 会话。

## 6. Sandbox、Workspace 与 Artifact

### Sandbox 隔离合同

每个执行实例必须满足：

- Kata/microVM 级内核隔离；非 root、只读基础镜像、临时可写层；
- 不挂载宿主根目录、容器运行时 socket、数据库凭证、模型主密钥或 Kubernetes service-account token；
- CPU、内存、进程数、临时盘、执行时长和输出大小硬限制；
- 默认禁止网络；需要联网的 Tool 经独立 egress proxy 按域名、解析后 IP、端口和协议 allowlist 放行；
- DNS/IP 重绑定二次校验，拒绝 loopback、link-local、metadata、私网保留地址和非 HTTP(S) scheme；
- 镜像按 digest 固定、签名验证和漏洞门禁；Sandbox Profile 记录 image digest、runtime class、egress policy 和 resource policy revision；
- 终止实例先撤销凭证和网络，再卸载 Workspace；异常实例不得复用给另一 Session。

### Workspace

Workspace 位于加密、多可用区 POSIX 存储，不使用 Pod 本地盘作为唯一副本。路径固定：

```text
sessions/<session_id>/agents/<agent_id>/...
sessions/<session_id>/shared/<group_session_id>/...
```

Sandbox 只能通过 Broker 获得与 ExecutionPrincipal 对应的受限挂载。每次 File Tool 仍做规范化、软链接、真实路径、owner/group、generation 和 Workspace lease 校验；挂载成功不是授权替代。Workspace 每 15 分钟生成增量恢复点，Session 删除进入 7 天可恢复隔离区后再按 Retention 清除。

### Artifact

生产 Artifact 写入版本化对象存储，key 固定为不含账号或原始文件名的 `artifacts/<artifact_id>/<sha256>`；tenant/account/visibility 只保存于受权限控制的数据库元数据，授权不依赖 key 前缀。发布协议：

1. 数据库事务分配 artifact_id、最终 key 和 Artifact=`publishing`，但不对普通列表暴露；
2. 在 Workspace read/write lease 内读取源文件并计算 SHA-256；
3. 上传到仅允许 create-if-absent 的临时对象，服务端加密并读回 metadata/checksum 校验；
4. 以 create-if-absent 把相同字节写入不可覆盖的最终 key；若 key 已存在只接受 checksum 完全相同；
5. 数据库事务核对 Artifact 仍为 publishing 和预期 checksum，写 provenance、Artifact=`published` 与 Event；此时才对外可见；
6. DB 提交前崩溃留下的对象由 Reconciler 按 artifact_id/checksum 幂等完成或在保留窗口后清理；绝不把缺少最终对象的记录标为 published；
7. Retention 只按状态机增加 tombstone/availability 状态，不用新字节覆盖旧 key；恢复只允许验证同 checksum 的原字节，用户删除默认 7 天内可撤销，强制 Retention 不可撤销。

下载使用短期签名 URL 或 API 流式代理；授权在签名生成前完成，URL 最长 5 分钟，不包含原始文件路径或用户标识。

## 7. 网络与依赖治理

- Namespace 默认 deny ingress/egress；按 API→DB/Redis/Storage、Worker→Provider/Tool、Broker→Sandbox 的最小路径放行；
- Knowledge API/Worker 只通过私网访问 Qdrant；Qdrant 不开放租户公网入口；
- Provider、Tool、IdP、JWKS、KMS 和存储均定义 connect/read/total timeout、并发上限和熔断；
- Provider/Tool Credential 只由对应 Adapter 在调用时取得短期值，不进入 Prompt、CapabilitySnapshot JSON、Event 或 Trace；
- 外部回调使用独立 Endpoint、签名、timestamp/nonce、重放窗口和 provider_event_id 唯一键；
- 所有 TLS 使用组织认可的最低版本和 cipher policy；内部服务使用工作负载身份或 mTLS；
- 管理入口、业务入口和外部回调入口使用不同 route、rate limit 和审计策略。

## 8. 扩缩与容量保护

扩缩首先按资源 lane，而不是按 Chat/Plan/Group 或 Agent 名称。每个部署必须配置：

```text
global_concurrent_runners
per_tenant_concurrent_runners
per_session_concurrent_runners
provider_rpm/tpm/concurrent_streams
tool_concurrency_by_tool_key
sandbox_concurrency/cpu/memory/runtime/output
agent_invocation_depth/fanout/total_count
group_member/message/summary_epoch limits
run_token/tool/cost/wall_clock hard budgets
outbox_claim_batch/lease/retry/dead thresholds
```

Admission Control 在创建新执行工作前原子预留预算；达到软阈值告警和降速，达到硬阈值返回 429/明确失败，不以 OOM、无界队列或静默丢任务退化。Autoscaler 不能把尚未取得 lease 的 Pod 数量当成已完成容量。

## 9. 发布与回滚

生产制品必须绑定同一个 release manifest：

```text
git_sha
container_image_digests
schema_head
openapi_sha256
configuration_revision
deployment_profile=enterprise_production_v1
identity_profile
tenant_id
identity_provider_revision + workload_policy_revision
capacity_policy_revision + retention_policy_revision
artifact_bucket_policy_digest + ledger_seal_bucket_policy_digest
kms_key_versions
sandbox_profile_revisions
agent_version_ids + content_hashes
skill_version_ids + checksums
uat/security/load/restore evidence ids
```

发布顺序固定为：兼容性预检 → expand migration → 后端 canary → Worker canary → 前端 → 全量观察 → contract migration。Canary 不得同时改变 Schema、身份 Profile 和 Sandbox runtime。出现 P0/P1、错误预算 fast burn、数据不变量告警或 fencing 异常时停止推进；应用回滚只能回到与当前 Schema 兼容的镜像，数据库不执行自动 destructive rollback。

## 10. 故障处理原则

| 故障 | 平台行为 |
| --- | --- |
| API Pod/客户端断线 | 已提交 Run 继续；客户端用可靠 sequence 恢复 |
| Runtime Pod 丢失 | lease 到期，旧 Runner lost，新 epoch 从安全 Checkpoint 恢复 |
| Provider 超时 | 按 Attempt 事实判断重试；已有 payload/未知结果不伪造新决策 |
| Tool 响应丢失 | 先查远端 operation；非幂等且未知则 outcome_unknown |
| Redis 全失 | 停止 token delta，可靠 API/SSE 和后台状态继续 |
| Workspace 暂不可用 | Tool waiting_resource；不占 Runner；超过预算明确失败 |
| Artifact DB/对象提交分裂 | Reconciler 按 artifact_id/checksum 收敛，绝不覆盖既有对象 |
| Event/Audit 链或 seal 校验失败 | 冻结受影响写入和证据保留，定位最后可信 seal；不得重算历史，按安全事件恢复与调查 |
| MySQL writer 故障 | 停止新副作用，完成受控 failover；Worker 重新取得 lease 后继续 |
| 单可用区故障 | 剩余副本承载，Sandbox/Worker 在其他区重建 |
| 全区域故障 | 按 RPO/RTO 在 DR 区恢复；旧区域写入口保持关闭，防止双主 |

## 11. 实施完成定义

只有以下条件全部满足，才能称为“生产架构已实现”：

1. [OpenAPI](openapi-v1.yaml)、数据库迁移和状态机由 CI 自动做 enum/字段/错误码一致性检查；
2. 三节点 API/Worker、多可用区数据库/存储及 Sandbox 隔离按本文部署；
3. OIDC/OBO 正向、撤销、跨 tenant、错 audience、伪造 subject 和 confused-deputy 测试通过；
4. Runner/Tool/Outbox/Artifact 的崩溃窗口、重复投递、旧 lease、网络分区和迟到结果均有故障注入；
5. SLO、容量、RPO/RTO 和安全演练达到[生产就绪要求](../operations/production-readiness.md)的数值基线；
6. release manifest 与所有证据绑定 clean SHA 和镜像 digest；
7. 值班、告警、恢复、回滚、Secret 轮换和安全事件 Runbook 经实际演练。

本文完整定义“怎么落地”；是否已经落地只由同一 release 的实现和证据决定，不能由文档声明替代。
