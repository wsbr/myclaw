# MyClaw 企业生产就绪要求

本文定义从受控 UAT 进入企业生产前必须达到的非功能数值、证据和审批。生产部署方式以[企业生产参考架构](../technical/production-reference-architecture.md)为准，发布与故障步骤见[企业生产运行手册](production-runbook.md)；本文是准入合同，不代表这些能力已经实现。

## 当前结论

```text
uat_single_node: supported for controlled validation
enterprise_production_v1 design: defined
current implementation/evidence: not ready / not supported
```

这里的“not ready”只描述当前实现和证据，不表示生产技术方案仍待选择。生产组件边界、身份、数据、Sandbox、SLO、RPO/RTO、发布和故障语义已经形成实施合同；剩余工作是按合同开发、部署和演练。评估时必须分别报告 `design_contract_status` 与 `implementation_evidence_status`，不能把“未跑生产演练”误写成“架构仍未定义”，也不能反过来把“文档已定义”写成“系统已经生产可用”。

第一阶段单机 Docker Compose、Local Sandbox 和本地 Artifact 快照只能验证功能闭环与可靠执行原语。本文已经给出默认生产数值；项目 Owner 可以在具体客户合同中收紧但不能放宽。只要以下任一项目标未实现或没有绑定 release 的演练证据，就不得把部署描述为企业生产可用。

## 发布级别

| 级别 | 允许用途 | 明确不承诺 |
| --- | --- | --- |
| `dev` | 本地开发、自动测试 | 数据保留、可用性、安全边界 |
| `uat_single_node` | 受控用户验收、架构和业务假设验证 | HA、生产 SLO、已承诺 RPO/RTO、不可信代码隔离 |
| `enterprise_production_v1` | 经组织批准的正式业务流量 | 仅在本文 Gate 全部有绑定 release 的证据后允许 |

服务必须暴露 deployment profile，UI 和运维页面不得把 UAT 显示为 Production。

`enterprise_production_v1` 的发布单元固定为“一个控制域、一个企业 Tenant”。Schema 仍按 tenant-safe 复合 FK 实现并用第二个合成 Tenant 做跨租户负向测试，但同一控制域承载多个客户 Tenant 属于 Identity Stage 3，必须新增配额、公平调度、平台运营身份、租户级 Registry overlay、计费/合规隔离和对应 ADR 后才能声明支持。

## 1. SLO 与错误预算

以下是 `enterprise_production_v1` 最低 SLO，按滚动 30 天窗口计算，并在准入容量内测量。Provider 模型生成耗时另列端到端 SLI，不从平台 SLO 中隐藏；但 Provider 自身内容生成时长不计入“durable Run 创建”和“调度延迟”。

| SLI | 目标 | 测量边界 | 当前状态 |
| --- | --- | --- | --- |
| API 可用性 | `>= 99.90%` | 认证后业务 API；包含平台依赖的 MySQL/Redis 降级，不包含客户 IdP 无法签发新 Token | 目标已定义，证据缺失 |
| Session/Run 创建 p95/p99 | `<= 1s / <= 2s` | 请求到 durable Message/Run/WorkItem/Event/Outbox 提交 | 目标已定义，证据缺失 |
| Runner 调度延迟 p95/p99 | `<= 2s / <= 10s` | runnable commit 到 Runner lease started；不含 Provider 响应 | 目标已定义，证据缺失 |
| SSE 可靠事件延迟 p95/p99 | `<= 2s / <= 5s` | ExecutionEvent DB commit 到已连接授权客户端可见 | 目标已定义，证据缺失 |
| waiting action 恢复 p95/p99 | `<= 3s / <= 15s` | answer/approval commit 到新 Runner lease started | 目标已定义，证据缺失 |
| Artifact 发布/下载成功率 | `>= 99.90%` | 授权请求中 immutable publish 成功或 download checksum 正确 | 目标已定义，证据缺失 |
| 无重复副作用 | 0 已知 incident | operation key、Attempt 与业务对账 | UAT 门禁，生产需持续监控 |
| 权限隔离违规 | 0 | 身份、Group、Workspace、Artifact 和 Tool | UAT 门禁，生产需持续监控 |

模型生成质量、Provider 延迟与 MyClaw 平台可用性必须分别统计，同时给出端到端用户 SLI。不得通过排除所有外部依赖让平台 SLO 失去业务意义。

99.90% 月度可用性的错误预算约为 43.2 分钟。5 分钟窗口用于技术采样，滚动 30 天用于合规判定；14.4 倍预算消耗持续 1 小时触发分页，6 倍持续 6 小时触发分页和发布冻结，2 倍持续 24 小时创建整改。每个告警必须有 Service Owner 和 Runbook。候选版至少连续 14 天无 fast burn 且全部性能 SLI 达标，首个生产月继续按完整 30 天窗口验收。

## 2. 容量模型与压测

每个部署必须建立并验证容量表：

```text
peak authenticated requests / second
concurrent active / waiting Runs
concurrent Runner / ToolAttempt / AgentInvocation
model RPM / TPM / concurrent streams by Provider
Outbox commands / second and oldest age
ExecutionEvent writes / second and retention growth
Workspace + immutable Artifact bytes / day
database connections / IOPS / storage growth
SSE connections and egress
```

容量不使用脱离硬件、Provider 配额和客户负载的固定 TPS。生产准入按以下算法确定 `admitted_peak`：

1. 在目标生产规格、真实 Schema/索引和等价 Agent/Model/Tool mix 下逐级加压，找到连续 60 分钟仍满足全部 SLO、无 backlog 单调增长、无 OOM/副作用重复的 `sustainable_peak`；
2. `admitted_peak = min(forecast_peak × 1.5, sustainable_peak × 0.70, provider/tool approved quota)`；若 forecast×1.5 超过后两者，必须扩容或降低准入，不得签发生产容量；
3. 在 `admitted_peak` 连续运行 4 小时，数据库 CPU/IO/connection、Worker/Sandbox CPU/内存和 Provider 配额均保留至少 30% headroom；
4. 再以 `min(admitted_peak × 2, hard admission limit)` 运行 15 分钟 burst，必须有界排队/429 且 30 分钟内清空 backlog；
5. 每次新增 Provider、Tool、Sandbox runtime、Group fan-out 上限或硬件规格都重新计算；季度复核实际 p95 峰值和增长率。

压测必须覆盖：

1. 预期峰值的稳定运行；
2. 至少一个已批准的 burst/overload 档位；
3. Redis、一个 Worker、Provider、Artifact 存储和数据库副本故障；
4. Group fan-out、循环上限、summary wake 和大 Context；
5. 长 waiting 恢复、Outbox backlog 清空和 lease fencing；
6. 容量耗尽时的有界拒绝，不是 OOM、重复执行或静默丢任务。

结果必须记录 release SHA、数据规模、AgentVersion、模型、硬件、数据库参数、测试工具、p50/p95/p99、错误率和瓶颈。无代表性负载数据不得通过容量 Gate。

## 3. RPO、RTO 与数据恢复

`enterprise_production_v1` 使用以下逐类目标。RPO 从最后一个已确认写入到可恢复点计算，RTO 从正式宣告灾难到业务入口恢复并通过一致性检查计算：

| 数据/能力 | RPO | RTO | 恢复来源 | 验证 |
| --- | --- | --- | --- | --- |
| MySQL 领域状态、身份、Event、Outbox | `<= 5 分钟` | `<= 60 分钟` | 跨区副本 + binlog/PITR + 跨区域备份 | 隔离恢复 + 领域一致性检查 |
| Workspace 可变文件 | `<= 15 分钟` | `<= 4 小时` | 多可用区存储 + 增量 snapshot | 路径权限、generation、checksum |
| immutable Artifact body | `<= 5 分钟` | `<= 4 小时` | 对象版本 + 跨区域复制 | DB metadata 与 body checksum 对账 |
| Registry 包与 SkillVersion | `<= 5 分钟` | `<= 4 小时` | 内容寻址对象存储 + 跨区域复制 | AgentVersion 依赖可重装配 |
| Event/Audit integrity seal | `<= 24 小时` | `<= 4 小时` | 对象锁定 seal + KMS key 跨区域恢复 | 链头、签名、scope 和 hash schema 重放验证 |
| Secret/Key 配置 | `0（已提交版本）` | `<= 60 分钟` | 企业 Secret Manager/KMS 复制 | 轮换且不导出明文 |
| Redis provisional 数据 | 可丢 | 重建 | MySQL/重连 | 不影响权威状态 |
| OTel 诊断数据 | `<= 24 小时` | `<= 24 小时` | telemetry backend 自身保留/备份 | 不参与业务恢复 |

生产 Gate 要求：定期备份、跨故障域复制、加密、restore-only 凭证、校验和、保留策略、PITR（若目标需要）和隔离恢复演练。演练必须证明恢复后不会重复推进 terminal WorkItem、不会复用旧 lease、不会丢失已发布 Artifact 映射。

当前 UAT 只记录实际备份点和实际恢复耗时；没有演练证据时不能声称达到下表目标。

## 4. HA 目标架构门禁

生产实现必须符合[企业生产参考架构](../technical/production-reference-architecture.md)，至少包括：

- API/Frontend/Runtime/Tool/Maintenance 无本地唯一状态，按参考架构的最低副本跨三个可用区部署；
- MySQL 单写者、至少两个跨区 failover 副本、binlog/PITR 和跨区域恢复路径；
- Outbox claim、Runner lease、execution epoch 和 fencing 在多副本/时钟偏差下验证；
- Workspace 使用加密多可用区 POSIX 存储，Artifact 使用版本化对象存储；
- Redis HA 或明确降级，且仍不成为业务权威；
- IdP/JWKS、Secret Manager、模型 Provider 和外部 Tool 的超时、熔断、限流与降级；
- 跨可用区拓扑、PDB、`maxUnavailable=0` 滚动升级、expand/contract Schema 窗口和应用回滚策略；
- 单点清单为空，或每个保留单点都有业务批准的降级/RTO。

默认编排器、存储和 Sandbox 隔离方式已经在参考架构中确定。替代实现只有在 ADR 中证明同等 SLO、隔离、fencing 和恢复语义后才可批准；仅“部署了 Kubernetes”仍不构成 HA 证据。

## 5. 威胁模型

### 资产与信任边界

关键资产包括用户身份和数据、Agent Instructions/Memory、Tool/模型 Credential、业务副作用、Workspace/Artifact、AgentVersion/Skill 包、ExecutionEvent/Audit 和企业 AgentCard。

边界至少包括 Browser/API、企业 IdP、Agent API Key caller、Runtime Worker、Model Provider、Tool/Sandbox、文件/Artifact 存储、数据库/Redis、远程 Agent Gateway 和运维人员。

### 必须覆盖的威胁与控制

| 威胁 | 最低控制 | 发布证据 |
| --- | --- | --- |
| raw userId 冒充、API Key 泄漏、跨用户枚举 | UAT 明确接受；生产启用 `enterprise_oidc_v1`、禁用 raw Header，验证 issuer/audience/subject/scope/client 并映射内部 account | 身份迁移、认证/授权渗透和负向测试 |
| OIDC login CSRF、code interception、session fixation/盗用 | Authorization Code + PKCE(S256)、一次性 state/nonce、精确 redirect、opaque HttpOnly Cookie、同源 + Session-bound CSRF、短 TTL/轮换/撤销 | callback 重放、错 state/nonce/verifier、跨站命令、旧 Session 与 CORS 负向测试 |
| Agent-to-Agent confused deputy | 固定原始 `tenant_id + account_id`、记录真实认证 caller、目标重授权、Credential 不提权、return route 固定 | 跨 Agent 权限矩阵 |
| Prompt/Tool output injection | 外部内容不可信标记、Tool schema、Capability enforcement、敏感动作确认 | 注入/越权红队用例 |
| 跨 Agent 私有文件外泄 | 强制逻辑根、principal-aware File Tool、软链接/路径穿越防护 | 私有/shared/Artifact 隔离测试 |
| Artifact 被覆盖或换包 | immutable storage key、checksum、provenance、审计 Retention | 发布后修改/删除/下载校验 |
| Skill/脚本供应链 | 精确 SkillVersion/checksum、签名/来源、审批、强 Sandbox 路线 | 包篡改和版本漂移测试 |
| Local Sandbox escape | UAT 明示非安全边界；生产使用专用节点池上的 Kata/microVM、非 root、只读镜像、无宿主凭证和默认拒绝出站 | Sandbox escape/egress/资源耗尽安全评估 |
| Tool/Provider 重放与重复副作用 | operation key、Attempt、outcome_unknown、fencing | 故障注入与业务对账 |
| SSRF、恶意 URL/Webhook | allowlist/egress proxy、DNS/IP 重绑定防护、scheme/size/timeout 限制 | SSRF 测试 |
| Secret 泄漏 | Secret Manager、短期凭证、日志/Trace 脱敏、轮换 | secret scan 和轮换演练 |
| 恶意/伪造 AgentCard 或远程 Agent | 签名/来源、声明与证明分离、协议输入校验、最小共享 | 联邦互操作安全测试 |
| Event/Audit 篡改或越权可见 | append-only、RBAC、版本化 hash chain、每日 KMS 签名 seal、不可覆盖外部存储、visibility projector | 链/签名/对象锁验证、篡改注入与访问测试 |
| DoS、Group fan-out/cycle、成本失控 | rate/budget/depth/fan-out/context limits、背压、熔断 | overload 与预算测试 |
| 运维内部人和 break-glass | 职责分离、强认证、审批、限时访问、全量审计 | 访问评审和演练 |

生产前必须由安全 Owner 记录适用威胁、残余风险、责任人和复审日期。表中“最低控制”是方向，不是实现完成声明。

## 6. 可观测性与运维

必须具备：

- SLI dashboard、错误预算和分页告警；
- Outbox oldest age/dead、Runner lease lost/fencing reject、waiting age、duplicate/unknown outcome；
- account/API-key authorization deny、raw userId 使用审计、Group fan-out、summary wake retry、Workspace deny、Artifact checksum mismatch；
- Event/Audit chain verification failure、seal age、KMS signing failure 和 immutable seal write failure；任一链不匹配立即按安全事件分页；
- Provider/Tool 依赖健康与成本；
- 结构化安全日志和 PlatformAuditEvent，且无 secret/Prompt/原始 Chain-of-Thought；
- 每个告警的 Owner、Runbook、升级路径和演练记录。

Trace 只用于诊断，不是恢复或授权事实。观测系统故障不能让业务状态分叉；但生产 Owner 可以按风险选择 fail-safe 限流或暂停高影响操作。

## 7. 数据治理与合规

未被客户/法规覆盖时采用以下默认 Retention；客户策略只能通过版本化配置显式收紧或在法务批准后延长：

| 数据 | 默认保留 |
| --- | --- |
| active Session/Message/Run/Plan/Group | Session 存续期间；归档后 365 天 |
| Workspace | Session 存续期间；删除请求后进入 7 天恢复隔离区再清除 |
| published Artifact | 365 天；Legal Hold/业务记录策略可延长 |
| ExecutionEvent | 90 天在线 + 310 天加密归档（合计 400 天） |
| PlatformAuditEvent / break-glass | 400 天，append-only |
| Event/Audit integrity seal | 不短于其覆盖账本的最长保留期；KMS 签名且对象锁定，不可覆盖 |
| OIDC LoginTransaction / BrowserAuthSession | LoginTransaction 最长 10 分钟有效；terminal 事务和已结束 Session 的仅 hash 元数据保留 30 天，安全事件可按 Legal Hold 延长 |
| OTel Trace | 30 天；安全事件引用的脱敏片段按事件记录保留 |
| provisional token / Redis | 最长 24 小时，允许随时丢失重建 |
| Idempotency record | 普通命令 24 小时；高影响副作用 operation key 随业务记录保留 |
| 备份/PITR | 35 天在线恢复窗口；季度/年度归档由客户法规策略决定 |

删除流程先写审计 tombstone，再撤销在线查询、清理 Workspace/对象、匿名化或删除允许删除的正文，最后生成覆盖 DB/对象/索引/备份到期时间的删除证明。Legal Hold 优先于普通删除并必须有独立授权。不可删除的审计记录只能保留最小 ID、动作、时间和 hash，不保留非必要正文。

进入生产前还必须按目标企业和地域确认：数据分类、驻留、加密、Legal Hold、审计导出、供应商 DPA、模型数据使用设置、个人数据访问、更正和删除流程。

Memory、Prompt、Tool 结果、Artifact 和 Agent-to-Agent 共享必须有分级策略。`admin` 默认不拥有用户正文；break-glass 必须单独授权和审计。

本文不声称满足任何特定法规或认证；适用性由法务、安全和业务 Owner 共同签署。

## 8. 生产发布 Gate

只有以下证据全部绑定同一 clean SHA、镜像 digest、Schema head、配置版本和 AgentVersion 集合时，`enterprise_production_v1` 才可批准：

Release Manifest 还必须固定 tenant_id、identity/workload policy revision、capacity/retention policy revision、Artifact/ledger bucket policy digest、相关 KMS key version 与 Sandbox/Skill/Model/Policy 精确版本；字段清单以[生产运行手册](production-runbook.md)为准，不能只写一个无法展开的“当前配置”。

1. 功能/UAT 门禁通过，无 P0/P1；
2. OpenAPI、数据库和状态机契约一致；
3. SLO、错误预算和告警获业务/运维批准并完成观察；
4. 代表性容量与 overload/failure 测试通过；
5. 各数据类别达到本文固定的 RPO/RTO，并通过恢复与 failover 演练；
6. HA 单点审查与滚动升级/回滚验证通过；
7. 威胁模型、安全测试、Secret 轮换和残余风险签署完成；
8. 数据治理、保留、删除和供应商边界完成审批；
9. On-call、Runbook、事件响应和责任人已就绪；
10. 发布记录包含可访问的测试、Trace、备份、恢复和审批证据。

此外，`identity_profile=raw_user_id_v1` 是明确的生产阻断项，不能仅通过风险签字继续使用。生产必须配置 `identity_profile=enterprise_oidc_v1`；其中用户请求使用 `oidc_user` 或 `oidc_obo`，无用户自动化只能使用受限 `service_principal`。

任何仅文档声明、仅 Mock 通过或未绑定 release 的历史 UAT 都不能满足生产 Gate。客户合同可以收紧本文数值；若要放宽，必须定义新的非 `enterprise_production_v1` Profile，UI、API 和销售材料均不得沿用该生产等级名称。
