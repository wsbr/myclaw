# MyClaw 一期 UAT 修复复验报告

## 1. 验收结论

| 结论维度 | 状态 | 说明 |
| --- | --- | --- |
| 产品 UAT | **不通过** | Plan 自然业务场景通过；Chat 与 Group 无法以正常业务表达触发必需的 Platform Tool/Agent 协作。Chat 未生成 Artifact，Group 未创建持久澄清、财务调用、历史检索或图外拒绝事件，存在未关闭 P1。 |
| 历史缺陷复验 | **部分通过，存在未关闭项** | sealed 草稿、中文 Artifact 下载和前端健康通过；未观察到 incomplete chunked read/零输出，但 30 次 Proposal 出现 1 个最终失败；Group 多词检索未形成真实 ToolInvocation。 |
| 正式发布门禁 | **阻断** | 本地 UAT 已判不通过；同时没有固定 SHA 的自建 CI Run、JUnit/Artifact 链接和部署审批，不能标记为正式发布通过。 |

不通过的直接原因符合既定判定规则：任一核心模式需要内部 Tool 口令才能闭环，即判定不通过。技术控制用例成功不改变该结论。

用户于验收过程中要求暂停。本报告以 `2026-08-03 08:23:49` 只读快照为收口点：不会取消平台内未完成 Run，也不会等待其后续状态变化。暂停时 30 次 Proposal 为 25 个 draft-ready、1 个最终失败、4 个未收口；已发生的最终失败已经使 30/30 门槛无法达到。

## 2. 基线、范围与原则

- 验收日期：2026-08-03。
- 业务域：零售电商经营。
- 仓库：`/Users/wsbr/Desktop/project/myclaw`。
- Git 分支：`vibe_1`。
- 固定基线：`c93e270`。
- UAT UI：`http://localhost:8181`。
- UAT API：`http://localhost:8101`。
- Runtime Worker：2 个；另有 Sandbox Worker 和 Maintenance Worker。
- 模型：真实 `qwen3.5-plus`，不是 Mock。
- 操作边界：登录、装配、发布、Chat、Plan、Group、Tool 治理均通过 Web UI；API/数据库只读提取 UI 未完整展示的 ID、事件、版本、Trace 与冻结证据。
- 代码边界：未新增或修改平台 API、Schema、公共接口或产品代码；仓库变更仅为本报告及脱敏证据。
- 验收边界：本次工作是对开发修复后的版本做验收，不承担平台修复。发现的产品问题按实际结果登记，不通过调整代码、内部口令或二次配置掩盖失败。
- 隔离边界：未触碰上一轮 `8081/8001` 测试栈。
- 保留策略：UAT 容器、持久卷、Agent、会话、Plan、Group、Artifact 和 30 个 Proposal 全部保留，未执行清理。

验收开始时 UAT 栈健康、两个 Runtime Worker 在线，数据库为全新现场：只有一个已注册模型，没有 Skill、Agent、Run、Plan、Group 或 Artifact。

## 3. UAT 对象

### 3.1 模型

| 项目 | 实际值 |
| --- | --- |
| Model ID | `019fc637-af5d-7874-9f26-23c324a2a48d` |
| Registry Key | `uat-primary-qwen` |
| Provider | `openai_compatible` |
| Model | `qwen3.5-plus` |
| Endpoint | DashScope OpenAI-compatible Endpoint |
| 参数 | `temperature: 0` |
| 能力 | streaming、tool calling enabled |
| Credential | 注册表仅保存 `env://MYCLAW_REAL_LLM_API_KEY` |
| UI 连接测试 | 成功，44 ms |

报告、截图和证据不记录模型密钥、登录密码、Token 或 Authorization Header。

### 3.2 Skill

| 项目 | 实际值 |
| --- | --- |
| 名称 | `retail-evidence-analysis` |
| Skill ID | `019fc654-9a1c-7057-b1d4-8b5ed2ddf2a7` |
| 状态 | published v1 |
| 发布包 SHA-256 | `22ca95891134a8dd1c09f47482431d958e563473df1887fca6e0dca47329212f` |
| 源文件 SHA-256 | `b1e1472df9e3e9564ede6c7a14b05502aeaafa7644e4abc772b1d5869bb82a72` |

Skill 定义零售证据分级、售后判责、经营指标口径、事实/推断分离和风险输出格式。带已发布 Skill 的草稿测试 Run `019fc659-d2fe-7438-ad19-93a475300354` 成功创建并完成，证明上一轮 sealed snapshot 缺陷已关闭。

### 3.3 Agent

| Agent | Agent ID | 业务场景冻结版本 | 策略修正版 | Group | 绑定 |
| --- | --- | --- | --- | --- | --- |
| 零售经营总控 | `019fc65c-ff6a-7a27-896c-f6e76bb23f8a` | v1 `019fc65d-0cc7-7c00-ae7c-ad8637dd6bb6`；版本冻结证据 v2 `019fc678-60f4-72bd-a6d8-adef66d618dc` | v3 `019fc6a0-8359-7a2c-a112-29c2023304eb` | enabled | 其余四个 Agent |
| 售后判责专员 | `019fc659-4c9d-7dbd-993b-fc8b3d384f6e` | v1 `019fc65a-6ca8-709e-9d58-5bc99cafc292` | v2 `019fc69f-c9a6-7bb0-b05d-9aec88e2e6b2` | enabled | — |
| 供应链履约专家 | `019fc65b-dcc9-7ed9-9fca-7fc44f29a2e3` | v1 `019fc65b-ea32-763b-9aa2-367cc3ea3dcd` | v2 `019fc6a0-5b72-7775-8008-3247342f18ec` | enabled | — |
| 财务风控专家 | `019fc65c-3542-7a53-99c2-61ff6f0d7cf0` | v1 `019fc65c-4298-7457-ae12-d57211e90be7` | v2 `019fc6a0-68af-7597-96bb-30080678c54d` | enabled | — |
| 营销策略专家 | `019fc65c-8e5d-7eef-a6f9-2e2449faf177` | v1 `019fc65c-9bb1-791d-acfc-a526858f8255` | v2 `019fc6a0-75fd-7035-8be0-495599fb7bf1` | disabled | — |

五个 Agent 均为 enterprise 可见，使用 `local` Sandbox、真实模型和已发布 Skill。Instructions 使用业务语言并禁止编造数据、伪造工具/专家结果、泄露凭证或绕过权限。

复验收口时通过 Runner Snapshot 发现：业务场景使用的 v1（以及总控版本冻结用 v2）实际仍为 UI 默认策略 `history.recent`、`memory_write.none`、`memory_recall.none`，没有使用计划要求的三项策略。这是本次 UAT 装配偏差，不得把 UI 操作意图写成已生效事实。偏差发现后曾通过 UI 另行发布上表“策略修正版”；这属于验收对象配置调整，不是平台代码修复，而且没有用新版本补跑或覆盖已经失败的核心业务场景。五个最新发布版本现均为 `context.standard`、`history.summary_recent`、`memory_write.user_agent_facts`、`memory_recall.user_combined`；旧版本、原始 Run 和失败现场全部保留。

营销 Agent 通过 UI 完成停用、再启用，最终保持 published；Platform Audit 有对应生命周期记录。

## 4. 结果总表

| 场景 | 结果 | 产品判断 |
| --- | --- | --- |
| 模型连接、Skill、五 Agent 装配 | 部分通过；存在设置偏差 | 真实模型、Skill/Agent 发布及生命周期正常；业务场景冻结版本误用默认 History/Memory；另行发布的策略版本未用于补跑 |
| 带 Skill 草稿测试 | 通过 | sealed snapshot 历史缺陷已关闭 |
| Chat：高价值订单售后判责 | **失败（P1）** | 文字判责和 1217 元计算正确，但工具语法被输出为普通文本，未澄清等待、未读文件、未计算 Tool、未写/发布 Artifact |
| Plan：双十一智能家电大促 | 通过 | 四节点冻结 DAG 全成功，Owner 汇总包含全部经营闸门 |
| 30 次 Proposal | **失败（P1）** | 暂停快照为 25/30 draft-ready、1 个最终失败、4 个未收口；29 个首轮结构校验失败，30/30 门槛未达到 |
| Group：订单取消率异常处置 | **失败（P1）** | 能生成文字诊断和权限说明，但没有持久澄清、实际财务协作边、多词历史命中或图外拒绝事件 |
| 中文 Artifact 修复 | 技术控制通过 | 中文下载、UTF-8 `filename*`、正文与 SHA-256 均正确；不计自然 Chat 通过 |
| 版本冻结 | 通过 | 新 Chat 使用 v2，旧 Plan/Group 固定 v1 |
| Tool 治理 | 通过 | 停用后批准无 dispatch；`never` 下无确认成功；最终恢复 `always` |
| Worker 恢复/fencing | 通过 | 运行中重启实际 Worker，epoch 1 `lost`，epoch 2 接管 |
| 可观测性与安全 | 通过（截至暂停快照） | Event、Trace、Span、Invocation、Outbox 可读；敏感信息扫描 0 命中 |

## 5. 自然业务场景

### 5.1 Chat：高价值订单售后判责 — 失败

- Session：`019fc65f-7cd9-7831-ad06-ce5ef77ddbfa`。
- 三个 Chat Run 均显示 `completed/succeeded`，但这是文字生成成功，不代表业务闭环成功。
- 最终 Artifact 数：0。
- Timeline：只有 session、sandbox、message、run 等事件，没有 Tool、Confirmation、Clarification 或 Artifact 事件。

#### 完整用户对话

1. 用户：

   > 我在处理一笔高客单售后。订单 R20260803-1042，用户实付 1199 元，签收第二天反馈屏幕破损，目前只看到两张照片，退回运费预计 18 元。你先按公司的售后规则帮我判断责任，缺什么证据就直接问我；证据齐了以后把退款金额算清楚，再整理成一份可以下载的判责报告，文件名用“售后判责报告-R20260803-1042.md”。

2. Agent 实际回复：声称“先读取相关规则”，随后把 `<tool_call><function=read_file>...` 原样输出为普通消息；平台没有创建 `read_file` ToolInvocation，也没有进入澄清等待。

3. 用户：

   > 视频找到了，是从拆快递到第一次开机的连续录像，没有剪辑。封签完整，外箱也没有明显破损，第一次点亮前就能看到屏幕裂纹。

4. Agent 实际回复：输出事实、推断和缺失证据；结论为商家责任，支持全额退款或换货、商家承担 18 元退运费，并正确计算 `1199 + 18 = 1217` 元；继续以普通文字询问物流信息和序列号，没有创建持久 Clarification。

5. 用户：

   > 物流公司是顺丰，运单号 SF202608031042，商品序列号 SN-R1042-2026，物流轨迹没有异常扫描。就按现有证据生成并保存报告，不需要再等。

6. Agent 实际回复：把不存在的 `<function=save_file>` 调用及完整 Markdown 报告作为普通文本输出。内容还引用未在 Skill 中存在的“《零售售后规则》第 3.2 条”，属于无来源规则陈述。页面仍显示 `Artifacts (0)`。

#### 判断

业务结论本身大体合理，但用户要求的“缺证据直接问、金额算清楚、生成可下载报告”未形成真实能力链路。用户必须知道并输入内部 Tool 名称才能执行，违反自然交互验收原则，登记 UAT-RV-001（P1）。

### 5.2 Plan：双十一智能家电大促 — 通过

- Session：`019fc662-1ed0-7ef3-8c65-dec8af9266f2`。
- Run：`019fc662-1ef7-7a9a-a0ee-30546c9e192b`。
- Plan：`019fc662-1ef8-798b-8bcb-453a20b93da2`。
- Graph revision：2。
- Frozen hash：`2588e89a1f6caf05410527b51cb0cfa676bcf9facbdff8ad4e65d119bc40282a`。

用户输入：

> 我们准备做一轮双十一智能家电活动。可售库存 3000 件，预算最多 50 万，活动后的毛利率不能低于 18%，订单要在 48 小时内发出，退款率要控制在 5% 以下。请让供应链、售后和财务分别把方案算清楚，最后由你汇总成一份能直接拿去开会决策的执行计划。

UI 将 Proposal 编辑并冻结为四节点 DAG：

- `supply`：供应链容量。
- `service`：售后保障。
- `finance`：财务测算。
- `owner`：总控决策。
- 三个专业节点并行，均以成功依赖汇入 `owner`。

四个业务节点和 Owner 交付均 `succeeded`。Owner 汇总包含库存 3000 件、预算不超过 50 万元、毛利率不低于 18%、48 小时发货、退款率不高于 5% 的责任人、阈值和风险闸门。冻结后 AgentVersion 固定为 v1。

#### 5.2.1 30 次真实模型 Proposal — 失败

30 个轻量经营 Proposal 均通过 Web UI 提交，使用真实 `qwen3.5-plus`，没有通过 API 创建 Run。暂停快照如下：

- 25 个 Plan 进入 `awaiting_confirmation`，即 draft-ready。
- 1 个 Run 最终失败并将 Plan 标记为 `cancelled`。
- 4 个仍未收口（2 running、2 queued），按用户要求保留现场且不继续等待。
- 30 Run、30 Plan、30 Work Item 一一对应；每个 Run 最多一个 `plan.created`、一个 `plan.draft_ready`，没有观察到重复 Proposal 或重复 Work Item。
- 29 个 epoch 1 Runner 明确发生 `invalid_structured_output`；另 1 个 epoch 1 Runner 在 Worker 恢复用例中转为 `lost`。
- 未观察到 `incomplete chunked read` 或零输出错误，但 Run `019fc68e-daf5-7d12-a9b6-3c4ba011e584` 在自动结构修复后仍以 `JSONDecodeError` 最终失败。
- 25 个成功样本的端到端延迟：最小 352 秒、平均 1533.4 秒、P50 1579 秒、P95 2597 秒、最大 2706 秒。

因此，30/30 最终成功要求不通过；“未复现 incomplete chunked read”不能替代批量稳定性门槛。

### 5.3 Group：订单取消率异常联合处置 — 失败

- Session：`019fc667-ea9b-7765-8637-019c82a46724`。
- Formation Proposal Run：`019fc667-eac9-7602-bccc-4e1dcf8aab9d`。
- Formation revision：2。
- Content hash：`87fda96f098f8f2a6e90218885c6347d38d14f6b69ee9569c5bb6b8476bc0387`。
- 首个 GroupRun：`019fc669-c404-79e2-96c3-5b308af42837`，outcome `succeeded`。

Bootstrap 原文：

> 今天早上订单取消率从平时的 3% 升到了 11%，库存同步大约慢了 20 分钟，同时发现 47 笔重复扣款，客服工单也突然多了很多。请把供应链、售后和财务拉进来，先各自判断原因和风险，再给我一套今天内能执行的止损方案。暂时不要找营销。

Formation 只含总控、供应链、售后、财务；营销不在图内。Formation、Bootstrap 和 Agent Instructions 在用户首次补充前均不包含“华东仓”，本轮排除了上一轮数据污染。

#### 自然定向对话与实际回复

1. 用户发给供应链：

   > 现在还没确认是哪一个仓出问题。你先把这个关键点问清楚，再继续判断，不要自己猜。

   Agent 输出一份“关键信息澄清请求”，但 GroupRun 正常结算为 `succeeded`，没有 `clarification.required`、没有 Required Action，也没有让同一 Work Item 持久等待。

2. 用户随后发给供应链：

   > 故障仓确认是华东仓。

   Agent 新建独立 GroupRun 并输出华东仓诊断报告，而不是恢复提出问题的同一 Work Item；报告末尾错误写入交付日期 `2026-01-01`。

3. 用户发给总控：

   > 请让财务同事再核一次这 47 笔重复扣款需要留多少退款准备金；如果缺客单价，就明确写出缺口和测算公式。

   总控只生成一封“财务风控专家核查请求”和测算模板，没有调用财务 Agent。`agent_invocation` 中没有本次 Group 记录，只有 Plan 的三条专业节点调用。

4. 用户发给总控：

   > 刚才大家已经讨论过华东仓和取消率了。请从这次联合处置的记录里把相关原话找出来，并告诉我来自哪条记录。

   总控回复“当前会话未授权调用历史记录查询工具”，只引用 Bootstrap 的取消率，明确没有找到华东仓原话；没有 `search_group_history` ToolInvocation。

5. 用户发给总控：

   > 再请营销同事评估一下活动要不要暂停；如果当前联合组没有这个权限，就明确告诉我，不要绕过权限。

   总控文字说明营销不在联合组，并拒绝绕过权限。这一产品话术正确，但没有尝试 `invoke_agent`，因此没有产生要求中的 `agent_invocation.rejected` 事件；权限边界只得到模型陈述，未得到 Runtime 执行证据。

#### 判断

Group 的自然语言分析和权限说明具有业务可读性，但要求验收的持久澄清、同一 Work Item 恢复、财务实际协作边、多词历史命中与出处、图外拒绝事件均未执行。登记 UAT-RV-002（P1）。

## 6. 技术控制用例（不计产品 UAT 通过率）

技术控制 Chat Session：`019fc67b-2b7e-7b24-a3e3-f3df8cd7941d`。本节允许精确 Tool 契约，用于判断底层修复是否存在。

### 6.1 持久澄清与恢复

输入包含 `call request_clarification` 后，平台创建真实 Clarification，并显示“输入回答；只会恢复提出该问题的 Work Item”。用户回答后，Work Item `019fc67b-2c37-7112-931f-10267e1e73df` 从 execution epoch 1 升为 2 并成功；Clarification 状态为 answered。

结论：底层持久澄清修复有效；自然语言路径不可达。

### 6.2 计算、文件与中文 Artifact

- `execute` 真实输出 1217。
- `write_file` 创建 `售后判责报告-R20260803-1042.md`，SHA-256 为 `4637860fe6a5da97508c97e48e0962e6158b459b52519a9ac91d665398d070cf`。
- `publish_artifact` 发布 Artifact `019fc683-d9fe-7af7-9f36-407aeada606d`，397 bytes。
- 下载 HTTP 成功，UTF-8 正文逐字一致。
- `Content-Disposition` 同时包含 ASCII fallback 与 RFC 5987/6266 `filename*`。
- 随后的 `read_file` 最终成功读取同一中文文件。

结论：中文 Artifact 历史缺陷关闭；由于只在精确 Tool 契约下成功，不能关闭 UAT-RV-001。

### 6.3 Tool 治理

1. `execute` 调用进入 waiting confirmation。
2. 在 Tool 管理 UI 停用 `execute`。
3. 批准旧请求：ToolInvocation `019fc67d-aa35-79c1-b53e-64a198ac513f` 以 `tool_disabled` 失败，`attempt_count=0`，没有 Tool Attempt，证明未 dispatch。
4. 重新启用并将确认策略设为 `never`。
5. 新调用 `019fc681-9ece-72e1-96bf-4085e53c21ad` 无确认成功，真实返回 1217。
6. 验收结束前恢复 `enabled + always`，最终 config version 5。

### 6.4 Group 工具边界

技术控制消息包含 `You must call search_group_history`，但 Agent 仍只输出一段声称调用工具的 JSON，平台没有创建 ToolInvocation。该 GroupRun 的 Owner Work Item 已成功，settlement Work Item 仍在持久队列中；截至报告收口没有历史命中 sequence、来源 Agent 或图外营销拒绝事件。根据验收边界，不再追加技术口令或调整配置，按技术控制失败原样记录；该结果不改变自然 Group 已失败的判定。

## 7. 横切能力

### 7.1 版本冻结

总控发布 v2 `019fc678-60f4-72bd-a6d8-adef66d618dc`，Instructions 加入仅用于证据识别的 `UAT-V2` 标记。新 Chat Run `019fc679-6fcd-795a-9048-d6cd0eaaed94` 使用 v2，并回复：

> 当前运行版本为 UAT-V2；本次经营总控会关注的三个核心指标是库存履约率、毛利与预算偏差、售后退款风险。UAT-V2

已冻结 Plan 和全部既有 GroupRun 仍引用 v1 `019fc65d-0cc7-7c00-ae7c-ad8637dd6bb6`，未发现版本漂移。

### 7.2 Worker 恢复与 fencing

在真实 Proposal 的模型生成期间定位实际 Runner 所属 `myclaw-uat-runtime-worker-2` 并重启：

- Work Item：`019fc687-135e-7c87-a2ea-093df62e80be`。
- epoch 1 Runner：`019fc687-1361-7abe-af1a-f97c65b28531`，重启后转为 `lost`。
- epoch 2 Runner：`019fc689-4815-7040-b935-1cc10cfb301d`，由持久队列重新接管，随后因结构校验失败进入再次修复。
- 同一 Work Item 的 epoch 3 最终成功。

旧 Runner 被 fencing，新 epoch 沿用同一 Work Item；每个 Run 最多只有一个 `plan.draft_ready`，未发现重复终态。

### 7.3 可观测性与安全

验收现场可读取 Platform Audit、ExecutionEvent、Runner Trace、OTel span、AgentInvocation 与 Outbox；暂停快照为 ExecutionEvent 503、Trace 102（completed 67、failed 32、running 3）、Span 202（ok 198、error 4）、generation span 98、AgentInvocation succeeded 3、Outbox completed 103/pending 4、Platform Audit 42。关键 ID 见证据摘要。

敏感信息扫描结果：

- UAT 容器日志：模型密钥模式 0、Authorization Header 0、登录密码 0。
- ExecutionEvent payload：模型密钥模式 0、Authorization 字样 0、登录密码 0。
- Model Registry：只保存 `env://MYCLAW_REAL_LLM_API_KEY`。

## 8. 缺陷与风险

### UAT-RV-001 — P1 — 自然 Chat 无法触发必需能力

- 现象：正常用户表达被模型转写成 `<tool_call>`/`save_file` 普通文本；无 ToolInvocation、Clarification 或 Artifact。
- 影响：售后判责无法交付可下载报告，核心 Chat 场景不能闭环。
- 技术事实：只有输入精确 `call <tool>` 才能到达平台 Tool 授权路径。
- 规避方案：内部测试可使用精确 Tool 契约；正式用户不可接受该规避。
- 发布要求：自然意图必须能被安全地映射到已装配 Tool；不得要求用户知道 Tool key。

### UAT-RV-002 — P1 — 自然 Group 协作只生成文字、没有 Runtime 事件

- 现象：澄清、财务复核、历史检索和图外营销请求都被当作普通生成任务。
- 影响：没有持久 Clarification、同一 Work Item 恢复、实际财务协作边、多词命中或 `agent_invocation.rejected`。
- 技术事实：精确 `You must call <tool>` 才能授权委派/Group Tool。
- 规避方案：技术控制可验证底层实现；正式用户不可接受该规避。
- 发布要求：自然定向消息必须触发相应 Runtime 能力，并保留可观测事件。

### UAT-RV-003 — P2 — 模型在无工具执行时伪造能力语法与无来源规则

- 现象：Chat 输出不存在的 `save_file` 调用和未在 Skill 中定义的规则条款；Group 报告出现错误交付日期 `2026-01-01`。
- 影响：用户容易把普通文本误认为已执行结果或正式制度依据。
- 规避方案：人工核对 Timeline/Artifact；不应作为产品长期规避。
- 发布要求：未执行 Tool 时 UI/Runtime 应明确区分模型文本与已执行能力结果；对无来源规则和日期增加校验。

### UAT-RV-004 — P2 — Artifact 下载读租约使后续 read_file 暂时等待

- 现象：通过下载接口校验 Artifact 后，工作区保留约 5 分钟 read lease；紧随其后的 `read_file` 进入 `waiting_resource`，待租约到期后成功。
- 影响：下载后立即读取同一工作区会产生不必要等待。
- 规避方案：等待租约到期；无数据丢失或重复执行。
- 发布建议：检查流式下载完成后的 read lease 释放，以及并发 reader 是否应互相阻塞。

### UAT-RV-005 — P1 — 真实模型 Proposal 批量稳定性未达 30/30

- 现象：暂停快照中 30 个 Proposal 仅 25 个 draft-ready，1 个已经自动修复后最终失败，4 个未收口；29 个首轮 Runner 明确报 `invalid_structured_output`，另 1 个首轮 Runner 因恢复用例转为 `lost`。
- 最终失败：Run `019fc68e-daf5-7d12-a9b6-3c4ba011e584`，epoch 2 报 `invalid structured Plan proposal after repair: JSONDecodeError`，Plan 为 `cancelled`。
- 用户等待：25 个成功样本延迟最小 352 秒、平均 1533.4 秒、P50 1579 秒、P95 2597 秒、最大 2706 秒。
- 影响：明确的 30/30 发布门槛未达到；结构修复不能保证最终成功，用户可能等待约 45 分钟后仍失败。
- 现场处置：未重提、未调参、未扩容、未取消未完成 Run，按暂停快照原样保留。
- 发布要求：关闭最终结构失败并在同等真实模型、同等并发和固定基线上重新达到 30/30，同时给出可接受的延迟目标。

### UAT-RV-CONFIG-001 — UAT 设置偏差 — 业务场景冻结版本使用默认 History/Memory 策略

- 现象：Runner assembly snapshot 显示 v1 使用 `history.recent`、`memory_write.none`、`memory_recall.none`，与计划目标不一致。
- 影响：本报告不对 summary/memory 的业务行为作通过判断；自然 Chat/Group 需在策略修正版上重跑。
- 现场事实：曾通过 UI 另行发布五个策略版本；数据库只读复核五个最新版本均引用目标策略，但没有用这些版本补跑核心场景或改写原始结果。
- 性质：验收装配偏差，不归因于平台代码缺陷；保留全部旧版本和 Run 供审计。

## 9. 历史缺陷复验

| 历史缺陷 | 复验结果 | 证据 |
| --- | --- | --- |
| sealed 草稿快照 | 通过 | 带已发布 Skill 的 draft-test Run 成功 |
| 中文 Artifact 下载 | 通过（技术控制） | HTTP 成功、UTF-8 `filename*`、正文与 SHA-256 一致 |
| incomplete chunked read 零输出重试 | 未复现原错误，但批量门槛不通过 | 0 个 incomplete chunked read、0 个零输出；同时 29 个首轮结构失败、1 个自动修复后最终失败，暂停时为 25 成功/1 失败/4 未收口 |
| Group 多词检索 | 不通过 | 自然路径无 ToolInvocation；技术控制消息也只生成声称调用的 JSON，暂停时 ToolInvocation 仍为 0 |
| 前端容器健康 | 通过 | UAT frontend 容器保持 healthy，UI 可用 |

## 10. 发布建议

当前不建议进入正式发布：

1. 先关闭 UAT-RV-001、UAT-RV-002、UAT-RV-005 三个 P1，并使用同一组自然对话和同等 30 次真实模型样本重新验收。
2. 将“模型输出的 Tool 语法”和“已执行 Tool 事件”在产品层明确区分，防止误交付。
3. 复验 Group 持久澄清、财务实际协作边、多词历史命中与图外拒绝事件。
4. 提供固定 SHA 的自建 CI Run、JUnit、测试 Artifact 和部署审批后，才能评估正式发布门禁。
5. 验收使用的模型密钥应在复核完成后轮换。

## 11. 证据导航

- [修复复验证据索引](evidence/uat-revalidation-2026-08-03/index.md)
- [中文 Artifact 下载校验](evidence/uat-revalidation-2026-08-03/artifact-download-check.md)
- [事件与 Trace 摘要](evidence/uat-revalidation-2026-08-03/event-trace-summary.md)
- [30 次 Proposal 稳定性](evidence/uat-revalidation-2026-08-03/proposal-stability.md)
- [首次测试基线报告](phase-one-uat-2026-08-03.md)
