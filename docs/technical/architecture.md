# V1 物理架构（历史实现参考）

> **历史 V1 实现参考，非目标设计。** 本文只保留进程、存储和 Outbox 的迁移证据；当前目标架构以[第一阶段 Agent-native 交付技术方案](phase-one-agent-native-delivery.md)为准，不得从本文恢复旧执行语义或设计新接口。

本文记录V1的进程边界、单机部署、存储职责、后台命令和MQ演进基线。产品执行语义见[Runtime执行](../modules/runtime-execution.md)。

## 1. 进程边界

同一后端镜像提供不同启动命令，代码可共包但进程职责不得混合。

| 进程 | 允许职责 | 禁止职责 | Outbox Topic |
| --- | --- | --- | --- |
| `api` | REST、认证授权、短事务写入、事件查询、SSE、模型 Token 实时转发入口 | 执行长模型循环、脚本、后台轮询 | 只生产命令 |
| `runtime-worker` | Runner 领取、Prompt 装配、模型循环、Inline Tool 协调、等待/结算 | 长时间本地进程、Channel 投递 | `runtime.runner` |
| `control-worker` | 租约回收、取消/超时、异步回调结算、等待恢复、Plan/Group 调度 | 模型或脚本执行 | `runtime.control` |
| `tool-worker` | Durable Async ToolAttempt、外部轮询、回调、普通异步副作用 Tool | Agent 模型循环 | `runtime.tool` |
| `sandbox-worker` | `execute`、进程树管理、Workspace 独占锁、输出限制 | 普通 Runner | `runtime.sandbox` |
| `delivery-worker` | 已提交 NotificationDelivery 的原 Channel 来源投递和重试 | 改写 Run 结果、为 Web/API 制造 Delivery | `runtime.delivery` |
| `maintenance` | Skill 对账同步、History摘要、Memory提取/合并、过期租约扫描、Retention、Outbox清理、Trace导出 | 作为业务状态源 | `runtime.maintenance` + 定时扫描；必要时生产 control 命令 |

第一阶段开发部署允许 `control-worker` 与 `maintenance` 共进程，`tool-worker` 与 `sandbox-worker` 分开；验收部署必须保持 API、Runtime Worker、Tool/Sandbox Worker 三类资源边界。任何合并只改变运维进程数，不改变 Topic handler 或事务接口。未启用任何 ChannelPlugin 的核心部署不启动 delivery-worker，也不会产生 `runtime.delivery` 命令；一旦启用 Channel，delivery-worker 是该部署 Profile 的必需进程。

## 2. 单机部署拓扑

```text
Browser / API Client
        │ HTTPS
        ▼
Web static server ───────► FastAPI API
                              ├── REST command/query
                              ├── SSE: committed ExecutionEvent
                              └── Redis Pub/Sub subscriber
                                      ▲ best-effort token delta
                                    Redis
                                      ▲
                          MySQL 8 ◄────┼────► Local filesystem
                          authority    │      workspace / skills
                              ▲        │
             SELECT ... SKIP LOCKED    │
      ┌───────────────┬───────────────┬───────────────┐
 runtime/control   tool/sandbox     delivery       maintenance
      workers         workers        worker          process
```

第一阶段参考 Docker Compose 必需服务：`web`、`api`、`runtime-worker`、`control-worker`、`tool-worker`、`sandbox-worker`、`maintenance`、`mysql`、`redis`；Channel Profile 另启 `delivery-worker`。Redis 是实时 Token Bridge 的必需部署依赖，但不是执行可靠性依赖。所有后端进程挂载同一受限数据卷：

```text
/var/lib/myclaw/
├── sessions/<session_id>/workspace/
├── skill-packages/<skill_id>/<version_id>.zip
└── shared-skills/<skill_id>/<checksum>/
```

目录所有者是最低权限服务账号。API 只负责受控上传/下载；只有 Sandbox Manager 与文件 Tool 可以解析宿主机绝对路径。

## 3. 基础设施职责

| 设施 | 权威职责 | 不承担 |
| --- | --- | --- |
| MySQL | 所有领域当前状态、ExecutionEvent、Checkpoint、Outbox、幂等记录、Workspace 锁 | 大文件本体、逐 Token 流、Trace 长期分析 |
| 本地文件系统 | Session Workspace、Skill 包源、checksum 内容寻址共享目录 | 权限事实、Artifact 元数据、执行状态 |
| Redis（必需、非权威） | API 与独立 Runtime Worker 间的实时 Token Pub/Sub、短 TTL 限流计数 | 状态、事件续读、队列、Session Memory、可靠重放 |
| OTel Collector（可选） | Trace/Metric 接收和导出 | 恢复、SSE、审计事实 |

Runtime 把 delta 发布到 `myclaw:live:<session_id>`，消息含 session/run/runner/message placeholder ID、进程内递增 delta_no 和文本片段，不含 Prompt、凭证或长期正文。API 在建立已鉴权 SSE 后订阅对应 channel，并把 delta 作为无可靠 id 的 `message.delta` 转发。Redis 不保存可重放 Token 流；Pub/Sub 丢包、Redis 重启或 API 进程重启只使实时文本中断。Runtime 捕获 publish 错误并继续模型循环，最终 Assistant Message 与 `message.created` ExecutionEvent 仍由 MySQL 原子保证。API 连接在 bridge 降级时发送 `stream.degraded`，客户端显示“后台继续”并等待可靠最终事件。

## 4. Database Outbox 驱动

领域服务只调用 `ExecutionQueue.enqueue(tx, CommandSpec)`。该调用在当前事务插入 `outbox_command`，不直接唤醒进程。Worker 循环：

1. 按自己的 Topic 与 `available_at <= now()`，使用 `FOR UPDATE SKIP LOCKED` 领取一批 `pending/retry_wait` 行。
2. 短事务写 `claimed`、`dispatch_lease_owner`、`dispatch_lease_expires_at`、递增 `attempt_count` 后提交。
3. 从数据库重新读取 `aggregate_type + aggregate_id` 当前事实并调用短 Topic handler。
4. handler 以领域状态和唯一约束判定重复命令；`runtime.runner` 必须在独立短事务取得 Runner lease/fencing 后立即 acknowledge Outbox，再在 Outbox claim 之外执行模型循环。
5. 对其他 Topic，“成功触发”同样只表示已取得 Attempt/Delivery 等领域执行权或确认无需动作；随后把 Outbox 标记 `completed`，不等待 Tool、脚本或投递完成。
6. 瞬时错误写 `retry_wait + available_at + last_error`；超出限制时，Session 命令写 `outbox.dead` ExecutionEvent，平台命令写 PlatformAuditEvent。业务实体是否失败由 handler 的领域事务决定。

`runtime.runner` 的固定切割点是：claim Outbox → 短事务校验 Runner/WorkItem并取得独立 Runner lease → commit → acknowledge Outbox → 执行模型。若 acknowledge 暂时失败，worker 不开始模型调用；命令重投看到 Runner 已 running 只负责完成 acknowledge，原 lease 过期后由 control 恢复。完整崩溃窗口见 [一致性](consistency-concurrency.md#outbox-与-runner-lease-切割)。

命令 payload 只允许路由所需小字段：`schema_version`、`action`、`expected_entity_version` 和关联 ID。Prompt、Tool 参数/结果、Agent 快照必须由领域表读取。

### Topic 与处理器

| Topic | 典型 action | 幂等判据 |
| --- | --- | --- |
| `runtime.runner` | `start_runner` | Runner 状态 + lease/fencing |
| `runtime.control` | `resume_work_item`、`reap_runner`、`apply_cancel`、`settle_run` | 实体状态 + version + cause ID |
| `runtime.tool` | `start_tool_attempt`、`poll_tool`、`apply_callback` | invocation/attempt 状态 + callback key |
| `runtime.sandbox` | `execute_process`、`cleanup_workspace` | invocation/retention operation ID |
| `runtime.delivery` | `deliver_notification` | delivery 状态 + provider message key |
| `runtime.maintenance` | `sync_skill`、`summarize_history`、`extract_memory`、`apply_memory_candidate`、`retention_cleanup`、`consistency_scan` | job/cause ID + 当前资源状态 |

## 5. Web、API、SSE 与后台执行

- Web 与外部 API 都先用 REST 创建/改变持久对象；HTTP 返回不等待业务完成。
- API 提交 Message 时，在同一事务创建 Message、Run、首个 WorkItem、Runner、ExecutionEvent 和 Outbox；返回 `202 Accepted`。
- SSE 连接只查询已提交 ExecutionEvent。`Last-Event-ID` 或 `after_sequence` 是 Session sequence，不依赖某个 API 进程内存。
- Runtime Worker 把 Token 发送到必需但非权威的 Redis live bridge；SSE `message.delta` 没有 Session sequence 或恢复保证。最终 `message.created` 才是可靠结果。
- 页面关闭、SSE 断线或 API 进程重启不取消后台执行。
- Clarification、图确认、Tool Confirmation 与取消均是独立 REST 状态命令；提交后由 Outbox 唤醒对应 WorkItem。

## 6. 维护任务

`maintenance` 使用数据库 advisory/lease 行确保单机只有一个同名 job 活跃，至少运行：Runner/Outbox/Workspace 锁租约回收（10 秒）、Skill 对账（5 分钟）、摘要与 Memory 待办扫描（30 秒）、Outbox/Checkpoint Retention（每日）、Workspace Retention（每日）。维护扫描只发现候选并写 control/sandbox 命令，不能跨长事务直接执行清理。

## 7. 未来 MQ 演进

保持不变的接口：

```python
class ExecutionQueue(Protocol):
    async def enqueue(self, tx, command: CommandSpec) -> OutboxCommand: ...
    async def claim(self, topics, worker_id, limit) -> list[ClaimedCommand]: ...
    async def acknowledge(self, command_id, claim_token) -> None: ...
    async def retry(self, command_id, claim_token, available_at, error) -> None: ...
```

接入 MQ 后，`enqueue` 仍只写 Transactional Outbox；新增 Relay 将 committed Outbox 发布到 MQ，并记录 `published_at`。MQ 消费者仍只携带 `command_id` 回库读取事实，handler、Topic、幂等键、状态机、ExecutionEvent 与 fencing 全部不变。Relay 发布成功但未记账、MQ 重投和消费者崩溃都按至少一次处理。第一阶段不预设 MQ 产品、分区或 DLQ 策略。
