# Agent 定时任务

定时任务只负责在指定时间创建一次标准 Chat Run，不引入第二套 Agent 执行引擎，也不引入 WorkDirective。任务只能由显式绑定了平台 `schedule_task` 工具的 Agent 在普通对话中创建：来源 WorkItem 的 AgentTurn 产生 ToolInvocation，Schedule 只引用这次调用并保存触发状态；到点后 `scheduler-worker` 写入唯一触发账本，再创建全新的 Run、WorkItem、Runner 和 AgentTurn。因此模型版本、工具治理、审批、Sandbox、审计和用量账本都沿用普通执行链路；平台定时唤醒不是用户发起的普通 Chat，不触发 Chat 后的 Group 语义涌现。

## 领域对象

- `tool_invocation.authorization_snapshot.agent_schedule_definition_v1` 保存规范化任务语义：Agent、名称、指令、上下文、版本策略和已解析的平台参与人；
- `agent_schedule` 只保存定义 ToolInvocation 引用、IANA 时区、UTC `next_fire_at`、次数上限/计数、重叠/误点策略和短租约；
- `agent_schedule_fire` 是每个计划时刻或“立即执行”的不可重复账本，唯一键为 `schedule_id + operation_key`；它冻结定义引用/hash 和 occurrence number，记录跳过、延后、分派、运行与终态，并关联实际 Session、Run 和 Runner；
- `runner_schedule_snapshot` 冻结触发时采用的定义引用/hash、Schedule revision、AgentVersion、时区和账号授权 revision，便于复盘；
- `user_session.origin_type=schedule` 与 `origin_schedule_id` 让普通会话可以追溯到任务定义。

任务指令以 `Message.role=user + sender_type=system` 落库：模型把它当作当前任务输入，界面显示为“定时任务”，记忆系统不会把平台触发内容误认成用户亲口陈述。`new_session` 创建可独立通知和追溯的新会话；`continue_session` 仅适用于当前入口 Agent，并把平台指令回灌原会话，以便继续使用对话材料。Runtime 会把本次触发指令标成唯一有效的当前请求，历史对话只作为背景，避免模型重新执行历史里的“创建定时任务”诉求。

## Agent 工具契约

`schedule_task` 是单个、可显式装配的平台工具，支持 `create | list | update | pause | resume | delete | run_now`。它不会自动加入所有 Agent；管理员发布代码工具后，Builder 在 Agent 草稿的“能力装配”中选中“定时任务”，再发布新 AgentVersion。

- 模型只在用户明确要求延时、提醒、周期执行或管理既有任务时调用；即时工作不调用；
- 相对时间使用 `delay_seconds`，由工具按真正执行时间计算；绝对时间必须包含 UTC offset，Cron 必须附 IANA 时区；歧义时间必须先问用户；
- `create/update/pause/resume/delete/run_now` 由动态风险规则强制逐次确认，`list` 不确认；
- 工具只能作用于当前 Tenant、账号、用户和当前 Agent，不能接收任意 `agent_id`；
- `definition_tool_invocation_id` 同时提供定义来源与创建幂等；语义更新会产生新的 ToolInvocation 定义版本并原子改指针，立即运行使用 ToolInvocation 派生 operation key；
- `max_occurrences` 是一等触发约束；“持续 10 次”精确物化 10 个计划 occurrence，不再通过模型估算 `end_at`；
- `principals` 只接受 Runtime 签发的 opaque `principal_ref`。定义保存稳定账号身份，触发前重新校验 Agent 可见边界并签发新引用，不能把过期签名当权限；
- 由 `authentication_source=schedule` 唤醒的 Run 只能查询，不能递归创建或改写任务；Runtime 不向模型展示该工具的变更能力，Dispatcher 也会在创建 ToolInvocation/Approval 前再次硬拒绝。

## 触发规则

支持三种定义：

- `once`：带 UTC offset 的单次时间；
- `cron`：标准五段 Cron，按任务 IANA 时区解释，数据库只存 UTC；
- `interval`：至少 60 秒的固定间隔。

版本策略默认为 `latest_published`，每次触发重新解析当前已发布 AgentVersion；`pinned` 固定创建任务时的已发布版本。无论选择哪种策略，分派前都会重新校验 Tenant、账号、成员关系、Agent 可见性和发布状态，Runtime 仍会重新解析 Tool/Policy/Sandbox 治理快照。

## 可靠性和多实例

1. Scheduler 用 `FOR UPDATE SKIP LOCKED` 领取到期定义，并写带过期时间的 lease；进程崩溃后其他实例可接管。
2. 同一个计划时刻先写 Fire 唯一账本，再推进 `next_fire_at`。重复领取只读到既有 operation key，不会生成第二个 Run。
3. Fire 分派也使用独立 lease；事务中原子创建 Session、系统指令、Run、WorkItem、Runner、RunnerSnapshot、ExecutionEvent 和 Runtime Outbox。
4. Scheduler 不执行模型或工具。它只创建短事务控制事实；所有长工作仍由有界 Runtime/Sandbox Worker 池执行。
5. Scheduler 恢复后不会按分钟展开长期积压。误点或重叠区间被合并为一条可解释账本，`fire_once` 补跑一次，`skip` 记录跳过并直接推进到未来时刻。

重叠策略为 `skip | queue_one | allow`。`queue_one` 在上次运行结束后最多补跑一次；更多重叠时刻记为 coalesced skip。暂停会取消尚未开始的 Fire，但不伪造取消已经运行的 Run；停用保留全部历史。

## 用户操作与通知

Web 的“定时任务”页面是管理面，只支持编辑触发时间/次数/运行策略、暂停、恢复、立即执行、停用和查看执行历史；任务语义在智能体对话中创建和修改。任务完成/失败后，Fire 终态作为站内未读通知进入侧栏铃铛；需要审批或澄清的定时 Run 仍使用统一 Required Action 投影。外部 Channel 投递继续使用 `NotificationDelivery`，不会因为 Web 定时任务制造伪 Channel 消息。

关键配置：

```text
SCHEDULER_POLL_SECONDS=0.5
SCHEDULER_WORKER_CONCURRENCY=4
SCHEDULER_CLAIM_LEASE_SECONDS=30
```

生产可横向启动多个 Scheduler 实例；正确性来自数据库行锁、lease 和唯一键，不依赖只有一个 Pod。
