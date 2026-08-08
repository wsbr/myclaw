# MyClaw 第一阶段 UAT 运行手册

> **运行级别：`uat_single_node`，不是企业生产。** 本手册保留已验证的 Compose、备份和故障处置机制，并按[第一阶段 Agent-native 交付技术方案](../technical/phase-one-agent-native-delivery.md)用于受控验收。它不承诺可用性 SLO、容量、HA、RPO/RTO 或不可信代码隔离；企业生产必须另行满足[生产就绪要求](production-readiness.md)。

本手册只覆盖[第一阶段交付边界](../modules/phase-one-scope.md)内的单机 Docker Compose 拓扑。Kubernetes、多可用区、外部 MQ 与独立 Artifact Store 不在本阶段范围内。单机故障可以导致整个 UAT 不可用；在测量前不声明恢复目标。

本地最终产品验收使用独立的 [`uat_single_node` 操作说明](../../deploy/uat_single_node/README.md)
和其中的 Compose。它使用 `myclaw-uat-single-node-*` 固定外部命名卷持久化 MySQL、
Redis 和 Workspace/Artifact 数据，不读取、迁移或删除旧 `myclaw-uat-*` 卷。
`test/docker-compose.test.yml` 只允许用于 CI/自动化测试，其 MySQL `tmpfs` 不得承载
产品验收数据。

## UAT 部署前门禁

1. 固定待发布 Commit SHA，确认工作树为空，并记录 `git rev-parse HEAD`。
2. 执行 Unit、关键模块 branch coverage、Frontend frozen-lockfile build、生产 Frontend image build、空库 Migration、双 Runtime Worker Mock Full-stack。
3. 使用 UAT 环境 Secret 注入真实模型 Credential，执行
   `deploy/uat_single_node/run-acceptance.sh` 的完整 API 与 Chromium 验收。Mock 结果不能
   替代真实模型 UAT 门禁。
4. 验证 `alembic heads` 只有一个 Head，并确认数据库备份已完成且校验和可读。
5. 检查镜像、日志、JUnit、Trace 与测试数据库不存在 API Key；只允许保存 `env://` Credential Ref。

## Secret 与配置

- `.env` 只能由部署系统在目标主机生成，权限应为 `0600`，不得提交。
- `SECRET_KEY`、`ENCRYPTION_KEY`、管理员密码、MySQL/Redis 密码和模型 Key 必须来自 Secret Manager 或受限环境变量。
- 模型注册只保存 `env://MYCLAW_MODEL_API_KEY` 形式的引用。不得把 Key 放入 URL、命令参数、Compose YAML、日志、Trace attribute 或测试快照。
- MySQL 备份工具只读取 `MYSQL_BACKUP_DEFAULTS_FILE` / `MYSQL_RESTORE_DEFAULTS_FILE` 指向的挂载 secret。defaults 文件格式为 `[client]` 下的 `user` 与 `password`，权限必须为 `0600`。
- UAT 目标进程必须拒绝默认 Secret、默认管理员密码、Debug、非法数据库池和不安全的 Runner heartbeat/lease 组合；是否已经实现以绑定 SHA 的自动测试和启动证据为准。
- 第一期 `identity_profile=raw_user_id_v1` 时，Agent API 只能暴露给受控内网调用系统；不得把 Agent API Key 下发浏览器、移动端或第三方。网关必须脱敏 Authorization，并记录 api_key_id 与 asserted userId 的安全审计关联。

## UAT 部署与回滚

1. `docker compose build` 构建固定 SHA 的镜像；记录镜像 digest。
2. 先执行备份，再运行 `docker compose run --rm api alembic upgrade head`。
3. `deploy/uat_single_node/up.sh` 启动 API、Frontend、两个 Runtime Worker、Sandbox Worker、Maintenance Worker、MySQL、Redis 和 Collector。
4. 验证 `/health`、Frontend `/healthz`、数据库连接、Redis 限流、Worker 心跳、Outbox backlog 和 Collector health。
5. Schema 迁移只允许前进。应用回滚必须使用与当前 Schema 兼容的已验证镜像；若迁移不可兼容，停止写流量后按恢复演练从备份恢复到新的空数据库，不得在原库试错。该流程只形成 UAT 恢复数据，不等于已承诺生产 RPO/RTO。

`init-env.sh` 从旧环境只读取真实模型连接信息，生成独立密码并绑定当前 clean SHA；旧 UAT 容器、卷和失败记录不参与新环境写流量。本次受控 UAT 的不可变 ModelVersion 将 `qwen3.5-plus` 固定为 `temperature=0`、`max_tokens=4096`、`extra_body.enable_thinking=true`，使用该混合模型的思考模式与自动 native tool calling。Provider 的思考模式不接受 wire-level 强制 `tool_choice`；因此 ConformanceRun 的普通消息、Tool continuation 和运行时调用保持冻结的思考模式，仅强制 Tool 选择的协议探针切换到同一模型公开支持的 `enable_thinking=false` 兼容变体，并在探针事实中保留该边界，正式 Agent Turn 不覆盖 ModelVersion 参数。同时冻结华北 2（北京）不超过 128K 输入档的公开表价（输入/输出分别为 0.8/4.8 CNY 每百万 Token）、价格生效日期与来源 URL。统计脚本在单次输入越档时直接失败，避免使用错误档位估算成本。

第一期新建 Group Run 的平台默认 `max_tokens=512000`，并与其余 Run 预算一起写入不可变
`hard_budget_snapshot`；Chat/Plan 默认仍为 `160000`。该 Group 上限依据真实五 Agent
容量流程观测峰值 `406730` Token 校准，保留约 25.9% 余量，同时继续受 160 Agent Turn、
240 Provider Attempt、128 Tool Invocation、64 Agent Invocation、16 Transport Retry 和
1800 秒 active time 的硬上限约束。修改默认值不得回写已有 Run；历史 `400000` 快照及其
`budget_exhausted` 失败事实必须原样保留。

最终演练调用 `deployment-drill.sh <previous-compatible-clean-sha>`：先生成备份，再临时切到
声明与 `0029` 兼容的上一镜像，除健康检查外必须真实登录并通过 Run ORM 列表查询；随后
切回最终 SHA 并重复同一查询。任何一步失败都由退出 Trap 尝试恢复最终镜像，禁止执行
Schema downgrade。

## 备份与恢复

最终 UAT 使用 [`backup.sh`](../../deploy/uat_single_node/backup.sh)：MySQL 以
`--single-transaction` 创建压缩逻辑备份，Workspace/Artifact 卷创建压缩快照和逐文件
SHA-256 manifest，两者均生成归档校验和。通用生产化脚本仍为
[backup_mysql.sh](../../deploy/scripts/backup_mysql.sh)。建议至少每日一次，并把备份与校验
文件复制到不同故障域；保留周期由企业策略确定。

恢复是破坏性操作，只能在已停止 API 与 Worker 写入、目标数据库和备份均复核后进行：

1. 在隔离环境创建空目标数据库，停止所有会写入它的 MyClaw 服务。
2. 设置 `MYSQL_RESTORE_DEFAULTS_FILE`、`MYSQL_RESTORE_DATABASE` 和精确值 `MYCLAW_RESTORE_CONFIRM=restore:<database>`。
3. 执行 `deploy/scripts/restore_mysql.sh /absolute/path/to/backup.sql.gz`。脚本先验证 SHA-256 和 gzip 完整性。
4. 对恢复库执行 `alembic current`、表数量检查和只读抽样；再启动一名 Runtime Worker，确认不会重复推进 terminal WorkItem。
5. 执行 Smoke Chat/SSE/Artifact 下载，随后恢复双 Worker。记录 RPO、RTO、备份时间、恢复时间、操作者与验证结果。

UAT 基线至少用 [`restore-drill.sh`](../../deploy/uat_single_node/restore-drill.sh) 将 MySQL
与 Workspace/Artifact 恢复到临时、全新的容器和卷，逐项核对 Head、表/Run/Artifact
数量、归档触发器、终态不复活及文件 checksum。脚本完成后删除这些临时恢复资源，不
覆盖当前或旧 UAT。未实际恢复验证的备份不能视为可恢复证据；在生产目标值、备份频率
和跨故障域方案明确前，RPO/RTO 记为“未承诺”。

## Retention 与 Cleanup

`maintenance-worker` 使用 MySQL advisory lock 保证单实例清理，并按批次删除已过期幂等记录、已完成/死亡 Outbox、terminal WorkItem 的旧 Checkpoint、非运行 Trace 和 terminal Delivery Attempt。每次执行写入不含被删内容的 `retention.cleanup` 审计。

UserSession、Run、WorkItem、ExecutionEvent、Artifact 与 PlatformAuditEvent 不由该任务删除。默认最短保留为：完成 Outbox 30 天、dead Outbox 180 天、Checkpoint 30 天、Trace 30 天、Delivery Attempt 90 天。缩短到配置下限以下会在启动时失败。

## 故障处理

| 故障 | 立即动作 | 恢复与验证 |
| --- | --- | --- |
| MySQL 不可用 | 停止入口流量和 Worker，禁止绕过权威库 | 恢复数据库；检查 Migration Head、Outbox backlog、WorkItem/Runner epoch，再逐步启动 Worker |
| Redis 不可用 | 限流与 token bridge 显式降级；数据库权威 API 继续服务 | 恢复 Redis 后验证 `X-RateLimit-State` 消失和 token bridge；客户端从 ExecutionEvent SSE cursor 续读 |
| 模型 Endpoint 异常 | 保留失败语义，禁止切 Mock 或静默换模型 | 核对 provider 状态、超时和限额；恢复后用新 Run 重试，不篡改已冻结版本 |
| Runtime Worker 失联 | 保留 lease 到期，禁止手工改 Runner 状态 | 替代 Worker 从 Checkpoint 以新 epoch 恢复；确认旧 heartbeat/result 被 fencing 拒绝且 terminal event 唯一 |
| Sandbox Worker 失联 | 不重复执行结果未知的外部副作用 | 检查 ToolInvocation/Attempt、幂等键和 `result_unknown`；由操作者按 Tool 语义决定重试 |
| Outbox backlog 增长 | 暂停非必要入口，保留数据库行 | 检查 claim lease/dead rows，扩至两个 Runtime Worker；禁止直接删除未完成命令 |
| OTel Collector 不可用 | 业务可继续，保留数据库 Trace 投影 | 恢复 Collector，验证新的 `runner_execution`、`model_generation` 和 Span Link；不把 Prompt/Key 加入诊断 |
| 磁盘容量告警 | 停止大文件入口，先备份 | 执行受控 Retention，检查 Artifact/Workspace；不得直接删除数据库或共享卷 |

## 限流与容量

API 使用 Redis 固定窗口限流，key 为认证主体或来源 IP 的 HMAC，不保存原始 token/IP。默认认证请求每分钟 600、匿名请求每分钟 60。Redis 不可用时保持数据库权威路径可用，并返回 `X-RateLimit-State: degraded`、写入告警；此时必须优先恢复 Redis 或在上游临时限流。变更阈值必须先做容量验证，不能通过关闭限流掩盖过载。

## UAT 部署后观察

- 执行 `deploy/uat_single_node/observe.sh`；正式证据不得缩短默认 1800 秒，输出写入同一 clean SHA 的 `observation-samples.jsonl` 与 `observation-summary.json`。
- 观察 30 分钟：5xx/429、模型错误率、Outbox oldest age、Runner lease expiry、Checkpoint recovery、Sandbox attempt 和数据库连接池。
- 抽查 ExecutionEvent sequence 连续、Run terminal event 唯一、OTel Span Link 存在且无敏感属性。
- 发现重复推进、版本漂移、凭证泄漏或恢复失败时，立即停止验收并按故障表处置。
