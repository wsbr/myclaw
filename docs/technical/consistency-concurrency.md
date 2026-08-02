# 事务、一致性与并发

本文是第一阶段原子性、幂等、执行所有权、事件顺序、Workspace 锁和竞态的唯一实现级定义。

## 1. 领域事务模板

所有 Session 执行迁移使用同一个 `UnitOfWork`：

```text
BEGIN (READ COMMITTED)
SELECT aggregate/session counter ... FOR UPDATE
校验 userId、expected version/state、fencing token
更新领域状态并 version+1
插入 ExecutionEvent（取得 Session sequence）
按需插入 OutboxCommand
COMMIT
```

无 Session 的 Agent/Registry/账号管理事务把 ExecutionEvent 替换为 PlatformAuditEvent，且不锁 Session counter。不得在事务内调用模型、外部 Tool、MQ、Channel 或文件进程。提交失败时领域状态、所属事件账本和 Outbox 全回滚；事件或 Outbox 唯一键冲突时读取既有迁移并按幂等处理。NotificationDelivery、Memory 写入是后续独立领域事务，失败不能回滚已完成 Run。

## 2. 幂等键作用域

| 操作 | Key 作用域 | 唯一约束/语义 |
| --- | --- | --- |
| 外部状态请求 | `(principal, operation, Idempotency-Key)` | 同 request_hash 返回原响应；不同 hash 409 |
| Channel 入站 | `(channel_account_id, external_message_id)` | 不重复 Message/Run |
| Runner 创建 | `(work_item_id, execution_epoch)` | 重复 resume 不建第二 Runner |
| Tool 语义调用 | `(work_item_id, business_idempotency_key)` | 同参数复用 Invocation；参数 hash 不同冲突 |
| ToolAttempt | `(tool_invocation_id, attempt_no)` | 物理重试有序且不覆盖 |
| Tool callback | `(tool_invocation_id, provider_event_id)` | 重复回调只返回已结算事实 |
| AgentInvocation | `(caller_work_item_id, idempotency_key)` | 模型重发同调用不重复目标 WorkItem |
| Clarification | `(work_item_id, idempotency_key)` | 不重复提问 |
| ExecutionEvent | `(session_id,event_dedupe_key)` | transition key 防重复；不同 Attempt/callback/cause 使用不同 key |
| PlatformAuditEvent | `(dedupe_key)` | 同管理命令最多一条审计；不占 Session sequence |
| Outbox | `(topic,dedupe_key)` | 同领域变化最多一个同类命令 |

Key 由服务端基于稳定语义生成，不能用随机 HTTP request_id 替代。Tool arguments 先 RFC 8785 风格 canonical JSON 后 SHA-256；浮点、时间和路径在 hash 前由 Tool Schema adapter 规范化。

## 3. 乐观版本与锁顺序

常规更新用 expected version；需要分配序号、冻结图、创建 Runner 或决策确认时使用短 `FOR UPDATE`。固定锁顺序避免死锁：

```text
UserSession → Run → PlanExecution/GroupFormationProposal/GroupRun → WorkItem → Invocation/Confirmation/Delivery → Workspace
```

跨多个 WorkItem 结算时按 UUID 字典序加锁。检测 MySQL deadlock/lock wait timeout 时，整个短事务最多指数退避重试 3 次；外部副作用不在该事务中，所以安全。

新 GroupRun 是一个特殊的版本解析事务：按 `UserSession → GroupSession → GroupAgentNode(UUID顺序) → Agent(agent_id顺序)` 加锁，重新读取 Agent 启停和 current published version 后，才插入 Run、GroupRun 及完整版本映射。Agent 发布/启停事务只锁 Agent 聚合，不得在持有 Agent 锁后反向锁 Session/Group；因此并发停用与创建以先取得 Agent 锁并提交者决定本轮快照，不会产生半冻结映射。owner 映射不可用时整个事务回滚；非 owner 不可用只写 unavailable 映射。

## 4. WorkItem 唯一 active Runner

创建 Runner 的唯一入口 `RunnerCoordinator.create_next`：锁 WorkItem，校验非终态、`active_runner_id IS NULL`、等待已满足；递增 execution_epoch，插入 Runner，回填 active_runner_id，写事件和 Outbox。Runner 结束时只有匹配 `active_runner_id + execution_epoch + lease_token` 才能清空指针。

重复 resume 看到 active_runner_id 时幂等结束。不同 WorkItem 可并行；Plan 节点、Group 节点和多个被调用 Agent 不受全局串行化。

## 5. Worker 租约与 fencing

- Runner/ToolAttempt/Workspace write lease 的 `lease_token` 每次领取均生成新 UUID；Workspace 另用递增 fencing_token。
- 默认 Runner lease 30 秒、每 10 秒心跳；ToolAttempt 按实现配置但心跳不超过 lease 的三分之一。
- 续租条件包含 `status=running AND lease_owner/token`。失败即停止写入和发起新外部调用。
- 每个写接口要求 `ExecutionFence(work_item_id,execution_epoch,runner_id,lease_token)`；数据库条件更新拒绝僵尸 Worker。
- control worker 只在 `lease_expires_at < db_now` 后标 lost。使用数据库时间，避免主机时钟漂移。
- 外部调用前先持久化 Invocation/Attempt，再调用；调用后回库时重新校验 fence。Fence 失效但外部已执行时，结果仍写到 Invocation 的回调/补偿入口，不得由旧 Runner直接推进 WorkItem。

## 6. Outbox 领取、确认与重投

领取批次建议 20，claim lease 30 秒。排序为 `priority DESC, available_at ASC, created_at ASC`。Handler 成功的定义是“已在数据库中可靠触发下一领域动作或确认无需动作”，然后才 complete Outbox。

重试采用带 jitter 指数退避，普通上限 10 次；control 恢复命令不因次数直接丢弃，达到上限后 dead + 平台告警并把相关实体转为明确 failed/lost（若状态机允许）。Outbox claim 崩溃由租约回收。MQ 演进后 MQ ack 只在数据库 acknowledge 成功后发生。

### Outbox 与 Runner lease 切割

`runtime.runner:start_runner` handler 只能执行以下短路径：

```text
claim Outbox（dispatch lease）并提交
→ 锁 Runner、WorkItem
→ 若 Runner=queued 且仍是 active_runner：CAS 为 running，生成 runner lease_token/expiry，写 runner.started
→ 提交 Runner lease 事务
→ 用 Outbox claim_token 标 completed
→ 开始模型/Inline Tool 循环，并只续 Runner lease
```

Outbox dispatch lease 只保护“谁处理这条命令”，Runner lease 才保护业务推进权。Outbox 不在模型、Tool、脚本或通知执行期间续租，也不等待这些工作完成。Outbox 可 completed 的条件只有：已经可靠取得对应 Runner/Attempt/Delivery 的独立执行权，或回读领域状态证明命令已由其他 worker 触发、已终态或不再适用。

崩溃窗口：

| 窗口 | 处理 |
| --- | --- |
| claim 后、Runner lease 前 | Outbox claim 过期回 retry_wait；Runner 仍 queued，重投正常取得 lease |
| Runner lease 后、Outbox ack 前 | 重投看到同 Runner running 且 lease 有效，不取得第二执行权，只幂等完成 Outbox；原 worker 可在成功 ack 后执行 |
| ack 后、模型前 | Runner 最终不 heartbeat，control 标 lost，并从安全边界创建新 Runner；Outbox 不复活 |
| 模型中 Runner lease 过期 | worker 必须停止新外部调用；control 回收，后继 Runner使用 Checkpoint/Invocation事实恢复 |
| 旧 worker 恢复写入 | 所有写入要求 runner_id+execution_epoch+lease_token，条件更新为 0 即 fencing conflict；外部迟到结果只能走 Invocation callback入口 |
| 重复命令遇 Runner terminal | handler 判定无需动作并 completed；不得创建新 Runner |

若 Runner lease 已提交但 Outbox acknowledge 因数据库故障无法可靠完成，worker不得开始新的模型请求；它先有限重试 ack，仍失败则退出，让 Runner lease 超时恢复。这样不会出现“长执行期间 Outbox claim 过期导致第二 worker启动”的耦合。

## 7. Session Event sequence

每个写事件事务先锁 `user_session` 行，读取 `next_event_sequence`，为本事务事件预留连续段并回写。事件与领域状态在同一事务提交，因此不存在“低 sequence 后提交”被 SSE 跳过的问题。

SSE 底层按 `session_id + sequence` 扫描，统一可见性投影过滤；客户端游标取 `scanned_through_sequence`，不是最后一条可见事件。只有不可见事件时也通过 cursor frame 推进，sequence 空洞不表示丢失。详细规则见 [事件与可见性](events-audit-visibility.md)。Group/Plan 并发只在极短提交阶段串行，不串行模型和 Tool。

## 8. Tool 至少一次与业务幂等

Outbox 只保证至少一次调度。Tool adapter 必须声明：`side_effect none/idempotent/non_idempotent`、`supports_cancel`、`supports_status_lookup`、`retry_policy`。规则：

- 只读或远端支持幂等键：同 Invocation 相同 key 可重试；
- 远端已返回确定失败且 retryable：新 Attempt；
- 请求可能到达但响应丢失：先用 provider_request_id/status lookup 查询；
- 无查询且非幂等副作用：Invocation `outcome_unknown`，不自动重放；
- Agent 业务上再次尝试必须创建新 Invocation，且 Prompt 明示上次结果未知。

Inline 也先落 ToolInvocation。只有确定无副作用、极短的本地转换可在同 Runner进程执行；文件与 `execute` 仍受持久 Invocation 和 Workspace 锁保护。

## 9. 多 Agent 并发与 all-settled

调用方 Runner 可在一个模型回合创建多个 AgentInvocation，赋同一 `batch_key` 后结束。每个目标拥有独立 WorkItem/Runner。每个目标终态事务写 control 命令；control 锁调用方 WorkItem，查询 batch 是否全部终态，只有最后一个完成者能创建恢复 Runner。单个失败不取消同批其他调用。

Group 定向消息只为 recipient node 创建 WorkItem，不广播唤醒。`search_group_history` 是普通只读 ToolInvocation；其查询快照通过调用时最大 event sequence 固定，避免一页结果内漂移。

## 10. Workspace 整体读写锁

锁由数据库公平 ticket 队列实现：

- read 请求仅在之前无 write 请求且无 active writer 时成批 grant；
- write 请求只有 ticket 前无 active/grant 请求且无 readers 时 grant；
- `execute` 必须 write；`read_file` read；`write_file` write；
- lease 过期由 control 标 expired，递增 writer fencing token 后授予下一项；
- Tool 每次实际文件系统访问前校验 lease；写入采用临时文件 + fsync + 同目录 atomic rename；
- Durable Tool 固化输入后释放锁，外部等待不持锁，写回时重新排队；
- Artifact 发布与 checksum 在同一 write lease 内完成，数据库登记在文件 atomic rename 后；登记失败保留未发布文件供维护清理。

第一阶段锁粒度是整个 Workspace，不允许跨模型回合持锁。稳定 ticket 防止写者或读者饥饿。

## 11. 取消与完成竞态

取消请求首先以 CAS 写 `cancellation_requested_at` 和事件，再由 control 传播。各完成写入按以下规则：

1. 若副作用/结果已在取消提交前确定完成，完成事实保留；上层不再创建后续工作。
2. 若取消先提交且操作尚未开始，转 cancelled。
3. 若执行中且远端支持取消，发送一次 cancel Attempt；最终按远端真实状态结算。
4. 无法确定结果时 outcome_unknown，不能伪装 cancelled。
5. Run 结算以全部已知结果计算；`cancelled` 只用于用户取消导致整体不再交付，已经可交付部分结果时可按产品命令进入 completed + partially_succeeded。

终态后的迟到结果只追加事件和 Attempt 诊断，不改 outcome、不写恢复 Outbox。

## 12. Tool Confirmation 即时策略

创建 Invocation 和真正底层执行前都读取 PlatformTool 当前 `enabled/config_version/confirmation_policy`：

- `never→always`：只要尚无 running Attempt，事务创建/复用 pending Confirmation，Invocation 转 waiting；
- `always→never`：新 Invocation 跳过等待；已有 pending 保持，必须由用户决策或取消；
- 参数改变：原批准不匹配 arguments_hash，必须创建新 Invocation/Confirmation；
- 已批准后的技术重试：同 Invocation、同 hash、同 principal，不重复确认；
- Tool 停用：未开始者 failed(tool_disabled)，已执行者按真实结果；重新启用不复活。

管理员不能替用户批准。策略更新使用 config_version，Confirmation 记录决策时看到的 policy_version，便于审计。

## 13. Run 与模式聚合状态权威

`run.lifecycle/outcome` 是 Chat、Plan 和 GroupRun 的唯一执行权威。`plan_execution` 只保存 definition_status，`group_run` 不保存 status/outcome；API 中的 Plan/GroupRun lifecycle/outcome 通过一对一 Run 读取，因此不存在三套可漂移镜像。

| 模式对象 | 定义/容器状态 | 允许的 Run 组合 |
| --- | --- | --- |
| Chat | 无额外定义状态 | Run 任一合法 lifecycle；completed 必有 outcome，cancelled outcome=NULL |
| Plan draft | definition_status=draft | proposal generation 后 Run waiting，wait_reason=graph_confirmation；生成期间可 running |
| Plan awaiting_confirmation | awaiting_confirmation | Run 必须 waiting/graph_confirmation |
| Plan frozen | frozen | Run 可 running/waiting/settling/completed/cancelled |
| Plan cancelled before confirm | cancelled | Run 必须 cancelled |
| Group Formation | Proposal 自己的状态 | generation Run 只反映生成周期，Proposal awaiting时它已completed；confirmed 后另建GroupRun/Run |
| GroupRun | 无重复状态 | 关联 Run 可 running/waiting/settling/completed/cancelled；`created` 由“Run=running且尚无Runner started”派生，`waiting_*` 由 Run.wait_reason 派生 |

Plan confirm 同一事务按 `UserSession → Run → PlanExecution` 锁顺序，把 definition awaiting→frozen、Run waiting→running，并创建 WorkItem/Event/Outbox。确认前取消同事务把 definition→cancelled、Run→cancelled。确认后的等待、结算、完成和取消只迁移 Run；Plan 定义不动。GroupRun 创建时必须与 Run 同事务一对一创建，之后只迁移 Run。

API 始终返回 `definition_status`（Plan 专有）以及 `run:{lifecycle,outcome,version}`；不返回伪造的 plan/group status 字段。Settlement 和取消只竞争 Run version/cancellation intent：先提交的完成事实保留，后提交者按最新 Run 收敛。Maintenance 每分钟检查上述组合、一对一缺失、completed/outcome 约束；发现异常写 PlatformAuditEvent `consistency.execution_state_mismatch` 并停止自动推进，第一阶段不自动猜测修复业务状态。

## 14. 隔离与故障边界

- 所有用户数据查询先绑定 `user_id`，再按资源 ID；Builder 所有权不进入 Session 查询授权。
- Trace 导出、实时 Token bridge、通知和异步 Memory 写失败均不回滚业务完成，只写可见或运维事件。
- Model/Tool 配置不可用时 Runner/Invocation 明确失败；不得 fallback。
- 数据库不可用时 Worker 不继续发起新外部副作用；已有外部结果通过同 Invocation 恢复入口补记。
