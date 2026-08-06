# Runtime 调度与数据库并发

本文是 ExecutionQueue、Database Outbox、Worker 所有权、租约恢复和领域状态并发控制的唯一权威来源。

## 设计目标

调度基础设施负责可靠地通知 Worker“某项工作可以开始”，但不保存业务任务的权威生命周期：

```text
OutboxCommand：要求某件事开始
Runner / ToolInvocation：记录这件事执行到哪里
WorkItem / Run：记录业务工作与结算结果
```

队列消息只传递命令 ID 和领域实体引用。Worker 必须从数据库加载当前事实，不能依赖队列中复制的 Agent、上下文或执行状态。

Runtime Worker 是长期运行的计算服务，Runner 是 Worker 在一段时间内取得某个 WorkItem 推进权后的领域执行记录。一个 Worker 可以先后承载多个 Agent 的 Runner；多个 Runner 也可以在不同时间、不同 Worker 上引用同一 AgentVersion。两者都不能作为 Agent 的稳定身份或状态源。

## ExecutionQueue 抽象

Runtime 只依赖统一接口：

```text
ExecutionQueue
├── enqueue(command)
├── claim(topics, worker)
├── acknowledge(command)
└── retry(command, available_at, error)
```

`enqueue` 必须参与调用方的领域数据库事务，并首先持久化 OutboxCommand，不能在领域事务中直接发布外部 MQ。第一阶段 `claim` 读取 Outbox 表；后期 MQ 实现由 Relay 发布已提交 Outbox，再由消费者完成 `claim / acknowledge` 语义。

第一阶段实现为：

```text
DatabaseOutboxExecutionQueue
```

Worker 直接轮询数据库中的 `outbox_command`。后期可以增加：

```text
MqExecutionQueue
```

此时由 Outbox Relay 将已提交命令发布到 RabbitMQ、Kafka、Redis Streams 或其他 MQ。MQ 选型、分区、消费组、死信队列和积压治理不属于第一阶段。

即使后期使用 MQ，Transactional Outbox 仍然保留。数据库事务不能依赖一次非事务性的外部 MQ 发布来保证不丢命令。

## 一张表，多种逻辑 Topic

第一阶段所有后台命令共用一张表，通过 `topic` 区分逻辑队列：

```text
outbox_command
├── runtime.control
├── runtime.runner
├── runtime.tool
├── runtime.sandbox
├── runtime.delivery
└── runtime.maintenance    # enterprise_production_v1
```

物理字段以[数据库契约](../technical/database-schema.md)为准，逻辑上固定为：

```text
id
tenant_id?
topic
command_type
aggregate_type
aggregate_id
partition_key
priority
cause_event_id?
dedupe_key
payload_json
available_at
status: pending | claimed | retry_wait | completed | dead
attempt_count
claim_owner / claim_token / claim_expires_at
last_error_code?
version
created_at
completed_at?
```

`payload_json` 只保存必要 ID/版本/原因引用，不复制大段 Prompt、Tool 结果或业务状态。

## Topic 职责

### `runtime.control`

处理短时控制命令：

- 取消与超时状态推进；
- Runner 租约回收；
- 异步 Tool 回调结算；
- Clarification、Approval 等等待条件满足后的恢复触发。

Control Worker 不能执行模型或脚本，避免控制面被长任务阻塞。

### `runtime.runner`

启动或恢复 Runner，执行上下文组装、模型循环和 Inline Tool。Runner 创建 AgentInvocation、ToolInvocation、Clarification 或最终结果后结束或继续。

### `runtime.tool`

处理 `durable_async` ToolAttempt、外部轮询和回调关联。不同 Tool 的并发上限属于 Platform Tool 配置，不由 Agent 覆盖。

### `runtime.sandbox`

处理 `execute`、Skill 脚本和其他本地进程任务。该 Topic 独立控制 CPU、内存、进程与 Workspace 独占锁，防止脚本阻塞普通 Tool 或 Runner。

### `runtime.delivery`

处理已经提交的 Channel NotificationDelivery。投递失败独立重试，不改变原 Run 的业务结果。

`uat_single_node` 可由平台定时任务执行 Skill 同步、Sandbox 回收和 Trace 导出；`enterprise_production_v1` 使用 `runtime.maintenance`，由多个候选 Maintenance Pod 竞争数据库 lease，不能依赖单一常驻 Pod。

Chat、Plan、Group、财务 Agent 或运营 Agent 不形成不同队列。Topic 只按执行资源和可靠性特征划分。

## Transactional Outbox

产生后台命令时，在一个数据库事务中完成：

```text
更新领域状态
→ 写 ExecutionEvent
→ 插入 outbox_command
→ COMMIT
```

因此只会出现“全部提交”或“全部回滚”，不会留下已经可见但永远没有调度命令的 Runner。

第一阶段 Worker 使用短事务和行级跳过锁定机制批量领取 `pending` 命令。领取事务只负责取得分发权，不能在执行模型、Tool 或脚本期间长期持有数据库行锁。

OutboxCommand 在成功触发对应领域执行后即可完成。长期状态由 Runner、ToolInvocation 或 NotificationDelivery 自己维护，不能在 Outbox 中复制一套执行生命周期。

## 至少一次与幂等

Database Outbox 和未来 MQ 都采用至少一次投递。以下情况可能产生重复命令：

- Worker 完成触发但尚未确认 Outbox 时崩溃；
- Outbox Relay 发布 MQ 成功但尚未标记 published 时崩溃；
- MQ 重新投递未确认消息。

消费者必须通过领域实体状态、版本号、幂等键和唯一约束拒绝重复推进。ExecutionQueue 不承诺恰好一次业务执行。

## 一个 Work Item 的推进权

同一 Work Item 同时最多只有一个 active Runner：

```text
WorkItem
├── active_runner_id
├── execution_epoch
└── version
```

不同 WorkItem 可以并行，因此同一 Session 的多个 AgentInvocation 和 Group 的多个成员不受此约束影响。

创建 Runner 时必须在一个事务中：

1. 校验 WorkItem 当前版本；
2. 校验没有 active Runner；
3. 创建 queued Runner；
4. 设置 `active_runner_id` 并递增 `execution_epoch`；
5. 写 ExecutionEvent；
6. 写 `runtime.runner` OutboxCommand。

Runner 结束、进入等待或丢失时，同样通过领域事务清除 `active_runner_id`。

AgentInvocation 目标使用独立 Work Item，因此同一调用方可以并行启动多个目标 Agent，但调用方 Work Item 自身仍只有一个推进者。

## Runner 租约与 fencing

Runner 执行所有权使用：

```text
status
lease_owner
lease_token
lease_expires_at
heartbeat_at
```

Worker 通过条件更新将 `queued` Runner 原子变为 `running`。只有更新成功的 Worker 可以执行；重复消息的其他消费者直接退出。

Worker 定期续租。租约过期后：

```text
旧 Runner → lost
WorkItem 不直接失败
→ 从最新安全 Checkpoint 创建新 Runner
→ 写 ExecutionEvent + OutboxCommand
```

旧 Runner 永远不复活。

仅有租约仍不足以阻止网络暂停后恢复的僵尸 Worker。每次执行权必须携带新的 `lease_token` 或递增 fencing epoch。Worker 写入模型结果、Message 或领域状态时必须提交该 Token；租约已被回收的旧 Worker写入必须被拒绝。

## 乐观并发控制

Run、WorkItem、Runner、ToolInvocation、AgentInvocation、Clarification 等可并发变化的实体维护 `version`。状态变化使用：

```text
entity_id
expected_version
expected_state
lease_token / execution_epoch（适用时）
```

条件更新失败意味着状态已被取消请求、异步回调或其他合法执行改变。调用方必须重新读取，不能覆盖新状态。

状态、ExecutionEvent 和后续 OutboxCommand 在同一事务提交。取消与完成竞争时，先提交的真实变化成为当前事实；后提交方基于最新状态结算，不能将已经完成的副作用改写为未发生。

## ExecutionEvent sequence

Session 内的 ExecutionEvent 使用单调递增 sequence。为避免并发事务取得序号后反向提交导致 SSE 游标跳过事件，分配 sequence 时使用 Session 级短事务串行点：

```text
锁定 Session event sequence
→ 分配一个或一段 sequence
→ 写领域状态、ExecutionEvent 和 Outbox
→ 提交并释放
```

这只串行化短暂数据库提交，不串行化模型、Tool 或 Agent 并行执行。

至少设置：

```text
UNIQUE(session_id, sequence)
UNIQUE(aggregate_type, aggregate_id, aggregate_version, event_type)
```

## 第一阶段调度策略

第一阶段采用：

- Topic 内按 `priority DESC, available_at ASC, created_at ASC` 领取；
- 仅区分控制命令和普通命令，不建设复杂业务优先级；
- 平台级全局 Runner 并发上限；
- 平台级每 Session Runner 并发上限；
- 不按 Agent 或业务模式配置队列；
- 不实现抢占式调度、租户权重或复杂公平算法。

这些限制属于平台 Runtime 配置，不属于 Agent 装配。
