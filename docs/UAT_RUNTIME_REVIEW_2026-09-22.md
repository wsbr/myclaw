# UAT 最近 48 小时运行轨迹复盘

统计窗口：**2026-09-20 11:22:22 至 2026-09-22 11:22:22（Asia/Taipei，UTC+8）**，左闭右开。下文时间均为 UTC+8，证据 JSON 保留 UTC。

环境：本机 Docker Compose `myclaw-uat`，UI 8181 / API 8101，数据库 migration `0049`。本次只读查询数据库、容器日志、源码与宿主电源记录；没有修改配置、业务数据、代码或重启服务。

结论：已记录的 HTTP 请求未出现 5xx/429，但异步运行有明确的模型工具参数错误、重复确认造成的审批等待、文件读取缺陷，以及长期失效的后台清理。优先修复这些已复现问题；本窗口没有支持增加 Worker 数量或激进压缩上下文的证据。

## 1. 运行概况与统计口径

| 指标 | 结果 | 解释 |
|---|---:|---|
| 新建运行 / 会话 | 37 / 11 | 全部来自 Web；无窗口前创建而跨入窗口的 Run |
| 运行完成 / 失败 | 34 / 3 | 完成率 **91.9%**；这是生命周期完成率，不是业务结果验收率 |
| 日期分布 | 9/20：0；9/21：34；9/22：3 | 窗口内样本集中在 9/21；9/22 包含 2 次连续失败 |
| 全部运行耗时 | 中位 27 秒；P95 366 秒；最长 4,116 秒 | 包含用户等待、审批等待及宿主暂停 |
| 完成运行耗时 | 中位 24.5 秒；P95 156 秒；最长 366 秒 | 34 个样本；不能当作模型执行耗时 |
| Provider 调用 | 143 次：139 成功、2 失败、2 outcome_unknown | 141 个 Turn；2 次零 payload 超时均在第二次调用恢复 |
| Provider 耗时 | 中位 6 秒；P95 26 秒；最长 79 秒 | 基于 ProviderAttempt.started_at / ended_at |
| 首个有效 payload | 中位 3 秒；P95 11 秒 | 141 个有 payload 的样本；包括工具参数片段，不等于用户首字 |
| 普通工具调用 / 执行尝试 | 38 / 41 | 33 个调用成功、5 个失败；协调动作不全部落入 ToolInvocation |
| Agent 委派 | 7 次 | 6 次成功、1 次随 Run 截止期限超时 |
| 已知输入 / 输出 Token | 1,021,675 / 40,104 | Provider 事实与 usage ledger 对账一致；4 个失败尝试 token 未知 |
| API 请求 | 32,009 | 5xx=0，429=0；另有一次登录 401、一次发布校验 422 |
| 执行事件 | 711 | 相关 Session 未见 sequence 缺口，窗口终态 Run 未见终态事件缺失或重复 |

分位数采用 **nearest-rank：排序后第 ceil(p × n) 项**；中位数按常规方法。SQL 时间精度为秒，毫秒级工具耗时显示为零不代表无耗时。当前快照没有未结束 Run，也没有 Outbox 待执行积压。46 条历史 `waiting_actions` Turn 均已有 ended_at，不能算作 46 个卡住任务。

## 2. 优先处理的问题

### P1：两次连续运行因工具参数 JSON 解析失败而终止

- 9/22 **11:17:32–11:18:52**，Run `01a0c71e-6230-7a32-a39c-008986619093`，耗时 80 秒。
- 9/22 **11:19:06–11:20:20**，Run `01a0c71f-ce7f-7a04-b793-b4bf632a8234`，耗时 74 秒。
- 同一会话中，用户请求详细技术方案，随后再次追问；两次均返回 `provider_schema_error`。Provider 原因是 `Provider returned malformed streamed Tool arguments`。
- 两次均已收到 payload；未记录成功解析的工具调用，`side_effect_committed=false`，Run 下没有已分发的工具动作。现有逻辑将 ProviderAttempt 标为 `outcome_unknown`，避免透明重放已有输出。
- 两次使用同一个 qwen3.5-plus 模型版本，关闭 thinking，`max_tokens=4096`。但错误路径没有保存 finish_reason、参数字节数或 JSON 错误位置，**不能据此确认是输出截断，也不能判定模型配置整体不可用**：该版本在本窗口还有 50 次成功。

建议：首先补齐脱敏的错误诊断（模型版本、工具名、参数长度、解析偏移、finish_reason、usage、request ID）；将输出截断与真正非法 JSON 分开。长文档写入采用分段输出/分段构建；经过能力验证后才使用严格 schema。恢复必须是有预算、有账本的新尝试，并先确认没有工具副作用，不能对任意已有 payload 自动重放或猜测补齐 JSON。

定位：[openai.py:174](/Users/wsbr/Desktop/project/myclaw/backend/app/core/model_hub/openai.py:174)、[openai.py:215](/Users/wsbr/Desktop/project/myclaw/backend/app/core/model_hub/openai.py:215)、[model_adapter.py:212](/Users/wsbr/Desktop/project/myclaw/backend/app/runtime/model_adapter.py:212)。前两份文件与运行容器哈希一致。当前解析异常发生在最终 done/usage 交付之前，会丢失本可用于诊断的结束信息。

验收：覆盖合法分片、中文转义、截断 JSON、错误 JSON、多工具部分成功等场景；同一详细方案场景能够交付或明确可恢复原因；有副作用时不重复执行。

### P1：用户已经回复同意，流程仍进入第二层审批，最终等待至失败

Run `01a0c370-bacb-7132-9ed3-5b17b8c6f77d` 的关键时间线：

| 时间（9/21） | 轨迹 |
|---|---|
| 18:09:00 | 用户要求执行脚本，Run 创建 |
| 18:09:50 | Agent 通过澄清消息要求批准创建并执行 |
| 18:10:46 | 用户回复“ok”，澄清已回答 |
| 18:11:06 | 子 Agent 的 write_file 再触发正式 session_grant 审批 |
| 19:09:00 | Run 的墙钟截止期限到达 |
| 19:17:36 | Run 失败、待审批取消、子 Agent 调用随之超时 |

正式审批等待 **3,990 秒（66 分 30 秒）**，没有人工决定；工具从未开始执行。自然语言回答 Clarification 不会创建 SessionGrant。之前已有的主 Agent 授权按 agent_id 隔离，也不会自动授权子 Agent。因此这是交互语义与权限范围不清晰，不能描述成工具执行超时，也不宜直接将自然语言“ok”扩大为所有 Agent 的授权。

另一个明确缺口是审批 API 将 `expires_at` 固定为 null，尽管数据库已保存 deadline。普通 11 次 session_grant 审批均在 8 秒内通过，说明这次不是普遍的审批接口性能问题。

建议：涉及具体工具操作时直接生成一次正式授权卡，避免先用 ask_user 请求批准、之后再要求审批；明确显示哪个 Agent、哪个工具、授权范围、截止时间。执行预算与人工等待预算分别展示，明确等待期限、提醒和过期后的恢复入口。跨 Agent 的授权必须由明确政策和用户选择决定。

定位：[required_actions.py:163](/Users/wsbr/Desktop/project/myclaw/backend/app/api/required_actions.py:163)、[required_actions.py:432](/Users/wsbr/Desktop/project/myclaw/backend/app/api/required_actions.py:432)、[clarifications.py:114](/Users/wsbr/Desktop/project/myclaw/backend/app/api/clarifications.py:114)、[action_dispatcher.py:965](/Users/wsbr/Desktop/project/myclaw/backend/app/runtime/action_dispatcher.py:965)。相关代码已与运行容器核对。

### P1：宿主机睡眠干扰 UAT 连续运行与超时处理

上述 Run 在 deadline 后 **516 秒**才结束。API 健康日志在 9/21 **19:02:06.861–19:17:38.424** 有 931.563 秒空档，覆盖 deadline；宿主同段有 Maintenance Sleep 与 19:17:32 DarkWake 记录。窗口内还明确记录了三次合盖睡眠。

这些证据高度支持宿主暂停影响处理时机，不能归因为已证实的应用死锁。48 小时中有 94 段大于 60 秒的健康日志间隔，但间隔总和不能直接当作精确宕机时间。“容器 Up 4 days、无重启”也不代表持续可服务。

建议：长期 UAT 部署到持续在线的主机/VM；本机验收明确运行与睡眠条件。加入宿主心跳、Worker 心跳新鲜度和 deadline 处理滞后指标。保持硬截止期限语义，另行优化人工等待预算；不能单纯延长所有 timeout 掩盖环境暂停。

### P1：后台清理与数据库不可删除约束冲突，整轮维护长期失败

窗口内 **19 次** `retention cleanup failed`，均为 MySQL 1644：`runner_checkpoint is append-only`。38 段 traceback 是异常包装，不是 38 次独立故障。

根因已确认：retention 删除过期 checkpoint，但 migration 0024 创建了 BEFORE DELETE 禁止删除触发器。各类别共用一笔事务，失败会回滚先前的 idempotency/outbox 清理，后续 trace/worker 清理也无法执行。相关三份源码与实际 maintenance 容器哈希一致。

- 本窗口 retention 成功审计 **0 次**；最近成功时间为 **9/7 18:44:56**。
- 快照已有 **1,057 条**满足 30 天清理条件的终态 checkpoint；总 checkpoint 1,343 条。
- 还有 **3,638 条**30 天前的 span；这证明积压存在，当前体量尚不足以称为磁盘事故。

建议：统一 checkpoint 的不可变与留存契约，用受限、可审计的归档/清理路径解决；不要直接去掉保护。将不同清理类别拆成独立事务/失败边界，增加每类最近成功时间、删除数与错误告警。

定位：[retention_service.py:90](/Users/wsbr/Desktop/project/myclaw/backend/app/services/retention_service.py:90)、[maintenance_worker.py:48](/Users/wsbr/Desktop/project/myclaw/backend/app/workers/maintenance_worker.py:48)、[0024 migration:194](/Users/wsbr/Desktop/project/myclaw/backend/migrations/versions/0024_agent_native_v2_runtime.py:194)。

验收：在真实 MySQL 触发器下清理过期终态数据；活跃恢复数据与审计事实仍受保护；人为令 checkpoint 清理失败时，其他类别仍可完成并分别记录结果。

### P1/P2：文件读取有可复现缺陷，冲突处理又放大了失败成本

**读取缺陷（P1）**：一次 read_file 设置 `limit=100`，按字节截断后直接 UTF-8 decode，切断中文多字节字符，误报文件编码非法。只读检查发现完整文件 2,718 字节、UTF-8 有效，但前 100 字节在 98–100 位置发生 `unexpected end of data`；当前文件 checksum 与历史成功 write_file 的结果一致，能够关联到同一文件版本。

修复应保证多字节边界安全：使用受限前瞻/增量解码，next_offset 按实际消耗字节推进，并定义任意 offset 落在字符中间的行为；不能用 errors=ignore 静默丢字。补中文/emoji 分页拼接、EOF、无效编码测试。另一例 read_file 是文件不存在，两者均被归为 `tool_execution_failed`，需要分别提供明确错误码。

**无效重试（P2）**：一次 `workspace_target_exists`、一次 `workspace_checksum_conflict`，各自动执行 3 次，产生 **4 次重复失败尝试**。write_file 将这些确定性冲突设为 retryable，Worker 因而重用相同参数；等待本身不能修改 create/replace 模式或过期 checksum。

将“可通过新动作恢复”与“原请求可安全重试”分开；冲突直接返回给 Agent 重新读文件/校验 checksum/构造新动作，保留 CAS。对临时网络故障才做有上限退避重试。

定位：[read_file.py:40](/Users/wsbr/Desktop/project/myclaw/backend/app/core/tool_registry/builtin/read_file.py:40)、[write_file.py:59](/Users/wsbr/Desktop/project/myclaw/backend/app/core/tool_registry/builtin/write_file.py:59)。详细只读复现与历史 checksum 见证据包。

## 3. 性能、成本与可观测性优化

### P2：减少多路轮询，先测单次会话请求量

32,009 个 API 请求中，健康检查 13,613 个；其余 18,396 个里，9 类 UI 查询有 **15,646 个，占 85.05%**，包括首次读取，不能全部视为浪费。working-plans 2,360 次、timeline 2,008 次、runners 1,619 次、tool-invocations 1,586 次。

空 plan 且 Run 非终态时每秒轮询；Inspector 将 waiting_user/approval 也作为活跃状态，同时每 1.5–2.5 秒查询多类数据。建议用已有 SSE 事件定向刷新，等待态退避，隐藏面板暂停，空 plan 限次并等事件恢复；保留断线后的低频兜底。以相同操作脚本的请求量、终态显示延迟对比验收，不能只看请求数下降。

定位：[Session/index.tsx:134](/Users/wsbr/Desktop/project/myclaw/frontend/src/pages/Session/index.tsx:134)、[SessionInspector.tsx:164](/Users/wsbr/Desktop/project/myclaw/frontend/src/pages/Session/SessionInspector.tsx:164)。

### P2：先修计量，再决定缓存、模型和成本优化

- 797 条 usage ledger 全部 `cost_status=unknown`，价格快照表为 0 条。**未知成本不是零成本**；先配置版本化价格与币种，历史修正用可审计 adjustment。
- 已知输入 Token 是输出的约 25.5 倍；单次成功调用输入中位 7,074，最大 13,484，低于本窗口所用最小模型输入上限 30,000。141 个 Turn 未见专用 compaction，history_summary 无新建/更新。没有证据认为本次失败由上下文撑爆导致。
- 一个完成的 Run 调用了 15 次模型、输入 114,223 Token；适合先减少找文件、冲突修复、重复确认的交互轮数，再评估上下文裁剪收益。
- `cached_input_tokens=0` **不能解释为真实缓存命中率为零**。Provider TokenUsage 与适配器只传 input/output，缓存字段沿途缺失后默认变成零，应传递供应商缓存字段，未知时标 unknown。
- 两条零 payload 超时记录 `error_code=NULL`，虽然 error_detail 是超时，第二次都已成功。需要在持久化前规范化错误码，补 retry_reason，避免错误分组失真。

定位：[model_hub/base.py](/Users/wsbr/Desktop/project/myclaw/backend/app/core/model_hub/base.py)、[openai.py:229](/Users/wsbr/Desktop/project/myclaw/backend/app/core/model_hub/openai.py:229)、[provider_attempt_recorder.py:59](/Users/wsbr/Desktop/project/myclaw/backend/app/runtime/provider_attempt_recorder.py:59)。

### P2：Span 不是调用账本，存在重复投影

真实 ProviderAttempt 为 143 条；`provider.attempt` span 有 91 条，是按 Runner 聚合的投影，fact_count 合计却为 144。一个 Runner 存在同 name、同 fact_fingerprint 的重复 span，造成输入重复 6,705、输出重复 588；`model_generation` 又是相同事实的别名，直接相加会进一步双计。

已证实重复存在；源码采用先查询后独立写入，缺少事实唯一约束，符合并发插入重复的风险，但现有日志不能还原具体竞争时序。建议使用稳定事实键与数据库幂等约束，明确快照更新/保留语义；账单、成功率与调用延迟继续以 ProviderAttempt / Usage Ledger 为准。聚合 span 的最早开始到最晚结束包含间隙，不能等同各调用实际耗时之和。

定位：[observability_service.py:228](/Users/wsbr/Desktop/project/myclaw/backend/app/services/observability_service.py:228)、[observability_v1.py:36](/Users/wsbr/Desktop/project/myclaw/backend/app/models/observability_v1.py:36)。

### P2：固定发布版本并补齐回归覆盖

当前镜像标记为 `worktree-887804fbe37d6af7`，本地 HEAD 为 `173f2e3`。这妨碍按提交重建和定位，但不能直接推断所有最新修复均未部署；本次关键路径已逐文件核对实际容器代码。后续 UAT 用 clean commit 构建，保存镜像 digest、migration head、release 与验收证据。

本窗口 schedule_fire 为 0，11 个 schedule 当前均 disabled/completed；不能用本次结果证明定时任务正常。verification_result 也没有窗口样本，34 个 completed 不代表 34 次通过业务验收。后续应增加定时任务、审批恢复、长文档交付的针对性样本。

## 4. 建议执行顺序与验收目标

| 顺序 | 工作 | 验收目标 |
|---|---|---|
| 1 | 文件读取边界、确定性冲突重试、清理触发器冲突 | 中文分页无损；冲突原参数只执行一次；各清理类别独立成功/失败，真实 MySQL 回归通过 |
| 2 | 模型参数错误诊断与长输出恢复 | 能区分截断/非法 JSON；失败信息完整；安全恢复无重复工具副作用；技术方案场景完成 |
| 3 | 统一正式审批交互、显示截止时间，稳定 UAT 宿主 | 不再“先口头同意再等待不明审批”；批准主体和范围明确；持续在线窗口验证 deadline 收敛 |
| 4 | Span 幂等、缓存 token 与价格计量 | Provider/ledger/去重投影对账；缺失计量不伪装为零；新调用可显示有来源的成本 |
| 5 | UI 等待态退避与 SSE 定向刷新 | 相同脚本查询量明显减少，终态和审批卡及时展示，断线能够恢复 |

当前 context.assemble 90 个 span 的平均耗时约 68ms；普通本地文件工具执行很短，最长 ToolAttempt 执行约 9 秒。结合没有现存队列积压与本窗口低样本量，应优先减少失败和无效动作，再通过同场景前后对比决定扩容、模型切换或上下文策略调整。

## 5. 证据与边界

证据目录：[runtime-review-20260922](/Users/wsbr/Desktop/project/myclaw/deploy/uat/evidence/runtime-review-20260922)。此目录已由仓库规则忽略，保存脱敏的数据库事实、聚合统计、只读采集脚本与分项复盘；不包含模型密钥、完整 Prompt 或原始业务文档。

- `db-evidence.json`：Run、Turn、ProviderAttempt、ToolAttempt、事件与查询口径。
- `metrics-summary.json` / `metrics-review.md`：分位数、token 对账、Span 重复与成本口径。
- `infra-summary.json` / `infra-summary.md`：日志分类、维护错误、健康日志间隔、电源记录、容器现状。
- `runtime-extra*.json`：审批、工具重试、状态与源码根因。
- `utf8-current-file-check.json` / `utf8-file-history.json`：字节边界复现与历史 checksum 匹配。
- `retention-evidence.json` / `retention-audit.json`：清理积压与成功审计。

这是历史检查与当前快照的组合：请求错误、Run 结果按固定 48 小时统计；容器资源、积压、文件字节验证、配置为检查时快照。没有连续性能采样，不能推断 48 小时 CPU/内存峰值；没有记录到连接池耗尽、死锁、OTLP 导出失败，不等于这些故障在所有时间均不存在。Qdrant 的 6 次错误为外发遥测上报失败，尚无知识检索故障证据。

本次执行的是只读核验与字节解码复现，表中的修复验收均为建议，尚未修改实现或运行修复后的回归测试。
