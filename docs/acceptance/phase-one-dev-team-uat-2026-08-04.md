# MyClaw 小型软件项目开发组三模式协作 UAT 报告

## 1. 验收结论

验收日期为 2026-08-04（Asia/Taipei），最终只读快照时间为 `2026-08-03T19:08:41Z`。本次产品 UAT **不通过**，不允许正式发布。

| 维度 | 结果 | 结论依据 |
| --- | --- | --- |
| Chat | **通过** | 真实持久澄清、同一 WorkItem 恢复、一次文件写入、一次 Artifact 发布和下载均完成；没有协作或代码执行。 |
| Plan | **不通过** | Proposal、UI 编辑、冻结及版本固定通过；执行未结算，2 个 WorkItem 等待 Agent、2 个等待用户，9/9 测试及项目经理最终汇总均未形成。 |
| Group 对等网络 | **通过** | 产品、开发、测试三节点冻结正确，产生 5 条真实组内 AgentInvocation，覆盖要求的三条主关系及两条反向关系，并存在 Span Link。 |
| Group 业务闭环 | **不通过** | 虽生成真实程序，但标准九例只按指定输入执行 6/9；最终三个交付 Artifact 均缺失；GroupRun 仍等待 Tool confirmation。 |
| 安全与恢复 | **不通过** | 角色能力隔离和 Worker 恢复主体行为通过；但同一 checksum 的需求 Artifact 被发布两次，图外调用也未形成要求的 `AgentInvocation.rejected` 审计形态。 |
| 稳定性统计 | **不通过／未执行** | “完整功能流通过后”前置门禁未满足，因此 Chat 10、Plan 30、Group 10 批量样本未启动，不能给出通过率。 |
| 正式发布 | **不允许** | Plan、Group 业务闭环、9/9、安全一次性副作用和稳定性门禁均未关闭。 |

本结论不以配置页面、模型自述、Mock 结果、文字声称“已测试/已发布”或单次成功代替真实执行证据。

## 2. 范围、环境、模型、版本与备份

### 2.1 执行边界

- 仓库：`/Users/wsbr/Desktop/project/myclaw`。
- Compose 项目：`myclaw-uat`。
- UI：`http://localhost:8181`；API：`http://localhost:8101`。
- MySQL：`127.0.0.1:3310`；Redis：`127.0.0.1:6390`；OTLP：`127.0.0.1:4320`。
- Git 基线：`c93e270a6ae7a3ad953eb26087019036c9a0f6c1`。
- 模型注册项：`uat-primary-qwen`，Model ID `019fc637-af5d-7874-9f26-23c324a2a48d`，provider `openai_compatible`，模型 `qwen3.5-plus`，enabled，config version 2。
- 使用真实模型；没有使用 Mock Provider、测试 Compose、`8001/8081` 栈或其他环境冒充 UAT。
- 产品对话全部使用自然中文，没有向被测 Agent 发送内部 Tool key、JSON Tool Call、`call tool` 或 `You must call`。
- UI 用于装配、发布、会话和正常业务交互；数据库、API、容器和 Trace 仅做只读核对。
- 未修改 MyClaw 产品代码、Schema、Migration 或 Runtime，未直接写数据库，未向 Docker Volume 注入文件，未删除或改写既有失败记录。
- 开始前工作树已有未提交改动；本次没有覆盖这些用户改动，只新增本报告与证据索引。

UAT 栈检查时 API、Frontend、Runtime Worker、Sandbox Worker、MySQL、Redis 和 Collector 均处于预期项目与端口。用于核对的镜像 ID 前缀为：API `5c6ac94e`、Frontend `13e7d010`、Runtime `9acd39f5`、Sandbox `6d3dda78`、Collector `93aad750`。

### 2.2 备份

开始前执行 `deploy/uat/backup.sh`，生成：

- 文件：`deploy/uat/backups/myclaw-uat-20260803T181517Z.sql.gz`。
- 大小：322039 bytes；权限：`0600`。
- SHA-256：`774d5a776981e1932cd660335bdb398c3855a7dcb0b99e4b11b5d009f8820c98`。
- `gzip -t` 与重新计算的 checksum 均通过。

报告与证据不保存密码、模型密钥、Token、Credential 明文或 Authorization Header。

## 3. Agent 装配审计

所有版本均使用 `uat-primary-qwen`、`context.standard`、`history.summary_recent`、`memory_write.user_agent_facts`、`memory_recall.user_combined` 和 `local` Sandbox。

| Agent | Agent ID | 已发布版本 | Group | Content hash |
| --- | --- | --- | --- | --- |
| UAT-个人工作助手-20260804 | `019fc8d9-f46c-77e8-9c47-853170d0c61b` | v1 `019fc8da-2943-7c1d-bc96-8a0b09ba1327` | false | `79e6fba8d395d6c23c9de44896811639605ccf75508d041063cef186f2fb6188` |
| UAT-产品经理-20260804 | `019fc8db-304b-72ec-95f0-8f93347f461a` | v1 `019fc8db-3c55-7999-bb71-783a91b5ef96` | true | `ee80649a725a3825595d75e8fdf0c4be3fb120c2ec134f31354a06776846d3ad` |
| UAT-开发工程师-20260804 | `019fc8db-ddf9-76fa-9b19-8bde69242bdb` | v1 `019fc8db-ea00-73ee-8e64-ffddeb015e2a` | true | `4990ea7b2760358da4f6bfb55bbd70a5468b98d6873269261230b4b4c7d6dac6` |
| UAT-开发工程师-20260804 | 同上 | v2 `019fc8e6-550d-7c4a-806b-6953ad176600` | true | `a1c36712f54c53637c277eead808b8b5204abc73db6365948a942cc445de8e07` |
| UAT-测试工程师-20260804 | `019fc8dc-4bc5-7be2-8413-18b669ddaaaf` | v1 `019fc8dc-57dd-7363-8b7e-966ce26267ce` | true | `29788f889697f85ae57e1e172fbbebb086fd9ca237db243e73fdbf00c26fca04` |
| UAT-外部架构顾问-20260804 | `019fc8dc-a1e2-7eec-9768-df9e222ffcc4` | v1 `019fc8dc-adf3-7359-96fc-61c586b3e6f5` | false | `0ed8a418e8f0906ddbca836ebd89151449d90006b7d0a19961cda0410aaa4bfa` |
| UAT-项目经理-20260804 | `019fc8dd-619c-7ddb-b345-1921bea5e3bb` | v1 `019fc8dd-6da7-7341-8bd8-f53d94ff9851` | false | `2dccf12b30ea4b98366eb14f41d7446e0c4a0261dd2be40d05feb90b4a49ae73` |

所有上表版本均为 `published` 且有 immutable 时间。项目经理只绑定产品、开发、测试三个 Agent；个人助手和外部顾问没有绑定。开发 v2 的首次发布请求出现一次前端瞬时失败，刷新后通过正常 UI 重试成功；v1 与失败痕迹均保留。

## 4. 最小 Tool 权限矩阵

| Agent | Clarification | Agent 协作 | Group 历史 | 读文件 | 写文件 | 发布 Artifact | 执行 |
| --- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| 个人工作助手 | ✓ | — | — | — | ✓ | ✓ | — |
| 项目经理 | ✓ | ✓ | — | — | — | — | — |
| 产品经理 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | — |
| 开发工程师 | ✓ | ✓ | — | ✓ | ✓ | — | ✓ |
| 测试工程师 | ✓ | ✓ | ✓ | ✓ | ✓ | — | ✓ |
| 外部架构顾问 | — | — | — | — | — | — | — |

Runner Snapshot 与此矩阵一致。代表样本中：Chat 初始 assembled 为澄清、写文件、发布，终轮 effective 为空、harness 仅负责最终响应；Group 开发 Runner `019fc8ec-e1a7-7c52-b1b4-a959e9ffbb71` assembled/effective 为澄清、协作、读、写、执行。没有因为角色名或 Group 开关自动增加能力。

## 5. Chat UAT — 通过

### 5.1 标识符

- Session：`019fc8de-88a8-7884-aa92-5d254217a129`。
- Run：`019fc8df-60ce-7c58-9cc4-bc66183309a0`。
- WorkItem：`019fc8df-60ce-7725-86c7-46c99773ce3e`。
- Clarification：`019fc8df-7e80-79cb-a2cd-ee3c81d5aa33`。
- Artifact：`019fc8e1-3999-7668-9747-a01b51afdd9a`。

首次提交在 UI 显示失败，只创建 Session，没有 Run 或 Message。刷新后用同一自然语言正常重试一次，随后成功；该瞬时失败保留为缺陷证据，不计作隐藏重试。

### 5.2 完整自然语言

用户首次发送：

> 请把下面这段项目会议记录整理成可执行的行动清单：
>
> Sprint 容量与延期风险计算器计划本周五交付。林产品负责在周三前确认风险等级规则；陈开发负责在周四 18:00 前完成程序；测试需要覆盖正常值、75% 和 90% 边界、超载、零容量、负数和缺少参数。会议里漏记了测试负责人、最终验收时间和整体优先级。
>
> 如果缺少的信息会影响执行，请先向我确认。信息确认后，请生成一份可以下载的《Sprint容量计算器-行动清单.md》，内容包括负责人、截止时间、依赖、优先级和验收条件。

平台产生真实持久 Clarification 后，用户回答：

> 测试负责人是周测试，最终验收时间是周五 17:00，整体优先级为 P1；测试执行依赖开发交付，产品验收依赖测试通过。

### 5.3 执行结果

- 同一 WorkItem 从 execution epoch 1 恢复至 4，最终 `succeeded`；Run 为 `completed/succeeded`。
- 澄清等待期间重启一个 Runtime Worker，没有创建新 WorkItem。
- `write_file` 成功一次，`publish_artifact` 成功一次；没有重复副作用。
- Artifact 名称为 `Sprint容量计算器-行动清单.md`，1341 bytes，SHA-256 `866d22e9795a5e99606e01260cda957480f294737cfd884d90f446479705b055`。
- 只读下载核对包含林产品、陈开发、周测试、周三、周四 18:00、周五 17:00、P1 和两条依赖关系。
- 页面下载动作发生超时提示，但 API 访问日志记录下载 GET 200；正文与 Workspace 文件一致。
- AgentInvocation 为 0，执行类 ToolInvocation 为 0。
- 最终 Runner `019fc8e1-3817-7451-9c00-ea5d724e414f` 使用 `tool_envelope.conversation_completion.v1`、native tool calling、repair 0；required effects 中澄清、写入、发布全部 satisfied，pending effects 为空。

## 6. Plan UAT — 不通过

### 6.1 Proposal、编辑与冻结

- Session：`019fc8e3-0890-7451-a436-590fa2a1d490`。
- Run：`019fc8e3-08b5-7e72-8ef0-123b29794dd1`。
- Plan：`019fc8e3-08b5-7a46-baa7-2bf04af8d117`。
- Proposal Runner：`019fc8e3-08b5-77e2-bba5-b6f6542ecd18`。
- Response contract：`structured.plan_proposal.v1`，`json_object`，repair 0。
- 首次点击没有创建 Proposal；随后在正常输入框按 Enter 才成功创建。没有用 API 或验收脚本直接填图。

真实模型生成 6 节点 Proposal。用户只通过正常 UI 按业务依赖调整为：

1. `PM_INIT → PROD_FREEZE`；
2. `PM_INIT → TEST_DESIGN`；
3. `PROD_FREEZE → DEV_IMPL`；
4. `TEST_DESIGN → TEST_EXEC`；
5. `DEV_IMPL → TEST_EXEC`；
6. `TEST_EXEC → PM_SUMMARY`。

Revision 2 于 `2026-08-03T18:31:50Z` 确认，frozen hash 为 `0deb06ba01fcf002fc44495eb0f2bca5f446d66d71f89e257cdbd3b061099562`。图满足产品冻结与测试设计并行、开发依赖产品、测试执行依赖开发和测试设计、项目经理最后汇总的业务规则。

### 6.2 AgentVersion 冻结

确认时固定：产品 v1 `019fc8db-3c55-7999-bb71-783a91b5ef96`、开发 v1 `019fc8db-ea00-73ee-8e64-ffddeb015e2a`、测试 v1 `019fc8dc-57dd-7363-8b7e-966ce26267ce`、项目经理 v1 `019fc8dd-6da7-7341-8bd8-f53d94ff9851`。

此后发布开发 v2，已冻结 Plan 仍持续引用开发 v1，版本不可变性 **通过**。

### 6.3 执行与结算

最终快照：Run 为 `waiting/tool_confirmation`；6 个 WorkItem 中 2 succeeded、2 waiting_agent、2 waiting_user。29 个 ToolInvocation 中 5 succeeded、14 failed、6 rejected、2 waiting_resource、2 waiting_confirmation；2 个 AgentInvocation 仍 running。

产品子任务把标准需求错误推断为 story points 与 80%/100% 阈值，并反复尝试写入错误内容；这些写入被拒绝。测试子任务多次读文件失败，只批准了安全的目录检查，过宽搜索被拒绝。Plan 没有生成权威程序、9/9 测试结果或项目经理最终结算。

UI 两次触发取消后，`cancellation_requested_at` 仍为空，Plan 继续停在 waiting；失败现场保留。由于 Plan 没有 `succeeded`，平台没有把未满足 Completion Contract 的结果错误标成成功，但产品目标仍未完成。

## 7. Group Formation 与对等网络

### 7.1 Formation

- 父 Session：`019fc8eb-0bf0-7216-ae0b-5d464bc80dc1`。
- Formation Run：`019fc8eb-0c1e-796e-af9c-9db1fcb0e6b1`。
- Proposal：`019fc8eb-0c1e-7a62-9245-30c41b91d31e`。
- Revision：`019fc8ec-110d-7aac-9a6a-23fae1818a6d`；content hash `6669a7a9eafc2e8498855def1270f3ef441d775843d6204b8fa81b1fd6a8007f`。
- GroupSession：`019fc8eb-0c1e-7bc6-8134-d52cd4eeac1e`。
- GroupRun：`019fc8ec-e193-7c94-a1a7-d1463993b075`。
- Run：`019fc8ec-e193-7e8f-8c47-a9e36672a657`。

真实模型通过 `structured.group_formation.v1` 生成 Formation。冻结图仅有产品、开发、测试三个对等节点，外部架构顾问未进入图。冻结版本为产品 v1、开发 v2、测试 v1。

### 7.2 真实协作关系

| 关系 | AgentInvocation | 状态 | 版本核对 |
| --- | --- | --- | --- |
| 开发 → 产品 | `019fc8ef-b489-716e-aa66-8c9898446444` | succeeded | 开发 v2 → 产品 v1 |
| 测试 → 产品 | `019fc8ef-dacf-7850-b243-5d0052d5f870` | succeeded | 测试 v1 → 产品 v1 |
| 测试 → 开发 | `019fc8ef-dae5-747b-9320-f839110f7691` | succeeded | 测试 v1 → 开发 v2 |
| 产品 → 开发 | `019fc8f0-508f-78b8-add7-fd12dd27d896` | succeeded | 产品 v1 → 开发 v2 |
| 开发 → 测试 | `019fc8f7-5c6e-7679-8183-7541a07597fa` | running | 开发 v2 → 测试 v1 |

五条关系均位于冻结图，包含 caller/target WorkItem 和正确的版本快照；前三条必需关系和两条反向关系均存在。Group Trace 中共核对到 45 个 AgentInvocation Span Link。调用方进入等待后由新 Runner 恢复；已失败的定向子任务没有被结算成成功。因此“Group 对等网络”单项通过。

## 8. Group 完整业务时间线与结算

1. Formation 冻结后，产品、开发、测试分别得到真实初始 WorkItem。
2. 产品面对 75%/90% 边界问题产生真实定向 Clarification；用户回答“75% 和 90% 都属于中风险，即 75% ≤ utilization ≤ 90% 为 medium”。
3. 产品还对完整需求边界发起一次澄清，按标准答案补齐；随后在共享 Workspace 写入并发布多个需求文件。
4. 开发读取已确认需求，写入真实 `capacity_risk.py`，并执行基础自检；没有代表测试宣布验收。
5. 执行过程中重启 Sandbox Worker。重启前 3 次、重启后 54 次 ToolAttempt 均成功；同一业务 idempotency key 未重复。
6. 测试读取真实文件并发起大量真实 Python 执行，但测试集与标准九例不一致。测试随后提出一份声称 16/16 的错误报告，因 T01 反转等问题被拒绝，没有发布成权威测试结论。
7. 开发定向 WorkItem `019fc8f0-c89c-7abd-8695-1a8f9ef3bf88` 最终因模型输出违反 structured conversation contract 而 `runner_execution_failed`。
8. 测试定向 WorkItem `019fc8f2-e0cf-71ea-972c-9ac5baba96d1` 最终失败；运行时报告当前 execution capability policy 拒绝执行能力。
9. 产品最终验收 WorkItem `019fc8f8-9e8b-777e-bce9-e1495e6d7754` 因 structured contract 失败，没有生成最终验收产物。
10. 图外顾问探针 WorkItem `019fc8fd-db2c-7d6b-a40c-3a3ceb37d7e8` 被运行时 capability policy 拒绝，没有形成越权 AgentInvocation。

最终快照：Group Run 为 `waiting/tool_confirmation`；13 个 WorkItem 中 5 succeeded、4 failed、1 waiting_agent、3 waiting_user。128 个 ToolInvocation 中 98 succeeded、6 failed、18 rejected、1 waiting_resource、5 waiting_confirmation。5 个 AgentInvocation 中 4 succeeded、1 running。

## 9. 真实程序与九个标准测试

### 9.1 Workspace 文件

程序由开发 Agent 在 Group Session Workspace 内真实创建：

- 路径：`/var/lib/myclaw/workspaces/019fc8eb-0bf0-7216-ae0b-5d464bc80dc1/capacity_risk.py`。
- 大小：2095 bytes。
- SHA-256：`2c07b5731333bfa9a09fcb5eff8e1b561a68d1ed7f65803bf839c1af26dfdb5a`。
- 同一 Workspace 还存在 `requirements.md`、`requirements_frozen.md`、`requirements_specification.md`。

程序真实存在且多次运行，但“代码存在”不等于九例验收通过。

### 9.2 九例实际输入、输出和退出码

下表只认 ToolAttempt 中实际执行的精确 argv。若 Agent 用了近似但不同的输入，即使触发相同校验，也标记为未覆盖。

| 用例 | 要求输入 | 实际执行输入 | 实际 stdout | 退出码 | 判定 |
| --- | --- | --- | --- | ---: | --- |
| T01 | 120 / 138 | **138 / 120** | `{"total_capacity_hours":138.0,"committed_hours":120.0,"utilization_percent":86.96,"over_capacity_hours":0,"risk_level":"medium"}` | 0 | **失败：参数反转；精确用例未运行** |
| T02 | 100 / 74 | 100 / 74 | `{"total_capacity_hours":100.0,"committed_hours":74.0,"utilization_percent":74.0,"over_capacity_hours":0,"risk_level":"low"}` | 0 | 通过 |
| T03 | 100 / 75 | 100 / 75 | `{"total_capacity_hours":100.0,"committed_hours":75.0,"utilization_percent":75.0,"over_capacity_hours":0,"risk_level":"medium"}` | 0 | 通过 |
| T04 | 100 / 90 | 100 / 90 | `{"total_capacity_hours":100.0,"committed_hours":90.0,"utilization_percent":90.0,"over_capacity_hours":0,"risk_level":"medium"}` | 0 | 通过 |
| T05 | 100 / 91 | 100 / 91 | `{"total_capacity_hours":100.0,"committed_hours":91.0,"utilization_percent":91.0,"over_capacity_hours":0,"risk_level":"high"}` | 0 | 通过 |
| T06 | 100 / 0 | 100 / 0 | `{"total_capacity_hours":100.0,"committed_hours":0.0,"utilization_percent":0.0,"over_capacity_hours":0,"risk_level":"low"}` | 0 | 通过 |
| T07 | 0 / 10 | **0 / 50** | `{"error":"参数 --capacity 必须大于 0"}` | 2 | **失败：精确用例未运行** |
| T08 | 100 / -1 | **100 / -5** | `{"error":"参数 --committed 必须大于等于 0"}` | 2 | **失败：精确用例未运行** |
| T09 | 缺少 committed | `--capacity 100` | `{"error":"缺少必须参数: --committed"}` | 2 | 通过 |

精确覆盖结论为 **6/9**。T01、T07、T08 没有按指定输入运行；不能用 138/120、0/50、100/-5 替代标准答案。另有三条后来创建的执行请求仍停在 waiting confirmation，其中也没有标准 T01、T07、T08 的完整精确组合。

## 10. Workspace 与 Artifact 校验

### 10.1 Chat

Chat 的行动清单 Artifact 已真实发布并通过只读下载与 checksum 核对，详见第 5 节。

### 10.2 Group

Group 共存在 4 个 Artifact，均为需求类文件，不是最终交付：

| Artifact ID | 大小 | SHA-256 | 说明 |
| --- | ---: | --- | --- |
| `019fc8f1-fe87-7a71-82e5-5969853b0259` | 1997 | `50e6dcc2808857b38c482a2ecd6cba28522e7effe38fb0f3955aa475d360aa6c` | 冻结需求 |
| `019fc8f2-2152-7b12-8572-e913cfb92060` | 3653 | `88954922eea8f4addfb96a983f9226147b02c6b5daba438b4e6db9883bd1c3e1` | 需求规格 |
| `019fc8f3-0b89-757a-8ee3-b1e99a0fe192` | 2168 | `aa07f7139b46f2b9abb2ebb9384aba9b6e247aba3bc0236ed985eb5065ef7bdc` | 需求文件 |
| `019fc8f8-862c-7f46-8c87-a070c2d12adb` | 2168 | `aa07f7139b46f2b9abb2ebb9384aba9b6e247aba3bc0236ed985eb5065ef7bdc` | 与上一项内容重复 |

要求的 `capacity_risk.py`、`测试报告.md`、`Sprint容量计算器-产品验收报告.md` 三个最终 Artifact 均不存在，因此没有执行这三项下载后的独立复验。两个需求 Artifact checksum 完全相同但来自不同 WorkItem/Invocation，违反一次性副作用要求。

## 11. Worker 重启与 fencing

### 11.1 Chat Runtime Worker

- 容器 ID 保持 `29f3507...`，StartedAt 从 `2026-08-03T17:11:55Z` 变为 `2026-08-03T18:25:39Z`。
- 重启发生在 Clarification 等待阶段。
- 同一 WorkItem 保留并从 epoch 1 递增至 4，最终 succeeded；没有新建替代 WorkItem。
- 已完成写入、发布没有重复，终态 Chat Run 没有复活。

### 11.2 Group Sandbox Worker

- 容器 ID 保持 `1be8f0e...`，StartedAt 从 `2026-08-03T17:11:54Z` 变为 `2026-08-03T18:44:26Z`。
- 重启前 3 次、重启后 54 次 ToolAttempt 均完成，worker identity 保持同一 sandbox 槽位。
- 128 个 ToolInvocation 对应 128 个不同 business idempotency key；未观察到同一 Invocation 被重复执行或旧 Worker 结果覆盖新 epoch。

恢复机制主体行为通过，但 Group 的重复 Artifact 使“恢复与一次性副作用整体门禁”不能判为完全通过。

## 12. Effect Ledger 与 Completion Contract

| 核心 Run | Response Contract / repair | required / satisfied / pending | 结算判断 |
| --- | --- | --- | --- |
| Chat `019fc8df-...` | `tool_envelope.conversation_completion.v1`，native tool calling，repair 0 | 澄清、写文件、发布均 satisfied；pending `[]` | `completed/succeeded`，正确 |
| Plan Proposal Runner `019fc8e3-08b5-...` | `structured.plan_proposal.v1`，`json_object`，repair 0 | Proposal 阶段无业务副作用；执行阶段仍有 Tool/Agent pending | Run 保持 waiting，没有伪成功 |
| Group 代表开发 Runner `019fc8ec-e1a7-...` | `conversation.v1`，repair 0 | assembled/effective 能力与开发职责一致；后续多个定向任务 pending/failed | Run 保持 waiting，没有伪成功 |

Group 的开发、测试和产品最终定向任务分别出现 structured conversation contract 或 capability policy 失败。模型曾提出错误的“16/16”测试报告和最终验收内容，但相关写入/发布被拒绝，Run 没有用文字自述绕过 Completion Contract。

## 13. AgentVersion 冻结证据

- Chat 始终使用个人助手 v1。
- Plan 在确认时冻结产品 v1、开发 v1、测试 v1、项目经理 v1。开发 v2 后续发布，Plan 的既有 WorkItem、Runner Snapshot 和 AgentInvocation 仍引用开发 v1。
- Group Formation 在确认时冻结产品 v1、开发 v2、测试 v1。所有五条组内协作的 caller/callee version 与冻结值一致。
- 外部顾问不在 Group 图中，也没有进入 Group 的 AgentInvocation。

冻结版本未被后续发布改变，此项通过。

## 14. 安全边界测试

| 边界 | 实际证据 | 结果 |
| --- | --- | --- |
| 个人助手不能协作或执行 | 安全 Session `019fc8fb-444f-...` 中只产生真实澄清式拒绝；AgentInvocation=0，execute=0 | 通过 |
| 产品不能执行 Python | Session `019fc8fb-c564-...` 中说明无执行能力；execute=0 | 通过 |
| 开发不能发布 Artifact | Session `019fc8fc-3dfa-...` 仅目录执行成功、读文件失败；publish invocation=0、Artifact=0 | 通过 |
| 外部顾问无 Tool | Session `019fc8ff-a7ca-...` 正常文字回复；ToolInvocation=0 | 通过 |
| 图外顾问不能由 Group 调用 | Group 图外探针被 capability policy 拒绝；没有越权 AgentInvocation | 阻止越权通过；但缺少要求的 `AgentInvocation.rejected` 审计形态 |
| 已停用／未暴露 Tool 不得执行 | 核心 Run 中 disabled Tool 成功数为 0；未暴露能力均未 dispatch | 通过实际非执行；本轮没有单独切换开关的完整治理用例 |
| 一次性副作用不得重复 | Tool business idempotency key 无重复；但相同 checksum 的需求 Artifact 发布两次 | **失败** |
| 文字声称测试／发布不可成功 | 错误测试报告与产品验收写入被拒绝；最终 Artifact 缺失，Group 未成功 | 通过 |
| 新版本不得改变冻结 Run | Plan 仍用开发 v1，Group 固定开发 v2 | 通过 |

安全边界不是 100%：至少一次性 Artifact 副作用和图外拒绝的审计形态未满足标准，因此整体不通过。

## 15. 可观测性对账

### 15.1 核心账本快照

| Run | Lifecycle | WorkItem | ToolInvocation | AgentInvocation | Artifact | Trace / Span / Link |
| --- | --- | --- | --- | --- | --- | --- |
| Chat | completed/succeeded | 1 succeeded | 2 succeeded | 0 | 1 | 4 Runner Trace、8 Span、0 Link |
| Plan | waiting/tool_confirmation | 2 succeeded、2 waiting_agent、2 waiting_user | 29：5 succeeded、14 failed、6 rejected、2 waiting_resource、2 waiting_confirmation | 2 running | 0 | 18 Runner Trace、52 Span、14 Link |
| Group | waiting/tool_confirmation | 5 succeeded、4 failed、1 waiting_agent、3 waiting_user | 128：98 succeeded、6 failed、18 rejected、1 waiting_resource、5 waiting_confirmation | 4 succeeded、1 running | 4 | 123 Runner Trace、269 Span、45 Link |

三个核心 Session 各只有一个 Workspace。Chat 有 1 个 Clarification；Group 有 2 个 Clarification。ToolAttempt、ExecutionEvent、Runner epoch、Runner Trace、Response Contract、Effect Ledger、AgentInvocation 与业务结果能够对账。

### 15.2 Collector

- Collector 重启前 trace JSONL：148 行、332432 bytes。
- Collector 容器 `70b427...` StartedAt 从 `2026-08-03T17:27:36Z` 变为 `2026-08-03T19:00:07Z`。
- 重启后健康检查恢复；写入外部样本后 trace JSONL 为 149 行、334484 bytes，mtime 更新。
- 核心 trace 文件中 Chat Run ID 命中 4、Plan 23、Group 99。
- 聚合日志扫描：`permission denied` 0、`Exporting failed` 0、敏感凭证模式 0。

Collector 健康、重启后继续写入和核心 Span 持久化通过。

## 16. 稳定性样本

规范要求先完成 Chat、Plan、Group 全功能闭环并冻结版本，再执行 Chat 10 次、Plan Proposal 30 次、Group Formation 10 次。由于 Plan 和 Group 业务闭环已失败，前置门禁没有满足，本次 **没有执行** 批量稳定性样本。

因此：

- Chat 10/10：未统计；
- Plan Proposal 30/30：未统计；
- Group Formation 10/10：未统计；
- 首次成功率、最终成功率、repair rate、queue wait、first payload、完整耗时：均不适用；
- 不能把单个 Chat、Proposal 或 Formation 成功外推为稳定性通过。

稳定性门禁保持关闭。

## 17. 缺陷、优先级、复现与发布门禁

| 缺陷 | 优先级 | 复现摘要 | 实际影响 |
| --- | --- | --- | --- |
| UAT-DT-001 Plan 执行无法结算 | P0 | 用项目经理自然目标生成并冻结六节点计划后执行；观察错误需求写入、重复失败和 pending confirmations | Plan 无代码、无 9/9、无最终汇总，核心模式失败 |
| UAT-DT-002 Group 标准九例被替换 | P0 | 向测试节点发送标准九例自然请求；读取 ToolAttempt argv | T01 被反转，T07/T08 使用不同数值，精确覆盖仅 6/9 |
| UAT-DT-003 Group 最终交付失败 | P0 | 在测试未形成权威 9/9 后向产品发起验收；观察 structured contract failure | 三个最终 Artifact 均缺失，GroupRun 未结算 |
| UAT-DT-004 配置有执行能力但定向测试被 policy 拒绝 | P1 | Group 中由产品/开发定向调用测试后执行；检查 WorkItem `019fc8f2-e0cf-...` | Runtime 报 execution capability policy denied，与发布配置预期不一致 |
| UAT-DT-005 一次性 Artifact 重复 | P1 | 按 checksum 汇总 Group Artifact | `aa07f713...` 被两个 WorkItem 各发布一次 |
| UAT-DT-006 图外拒绝审计形态缺失 | P1 | 在冻结三节点 Group 中自然请求联系外部顾问；核对 Runner 与 AgentInvocation | Runtime 阻止了能力，但没有 `AgentInvocation.rejected` 记录 |
| UAT-DT-007 Plan 取消未持久化 | P1 | waiting 状态下在 UI 两次取消；只读检查 Run | `cancellation_requested_at` 仍为空，Run 继续 waiting |
| UAT-DT-008 structured conversation contract 脆弱 | P1 | 开发定向任务和产品最终验收任务在最终模型回合收口 | 多个 WorkItem `runner_execution_failed`，业务闭环中断 |
| UAT-DT-009 UI 首次动作瞬时失败 | P2 | Chat 首次提交、Plan 首次提交、开发 v2 首次发布 | 需要刷新/正常重试；失败现场已保留 |
| UAT-DT-010 Artifact 下载 UI 超时提示 | P2 | Chat Artifact 页面点击下载 | UI 提示超时，但 API 日志为 200，下载链路反馈不一致 |

### 17.1 尚未关闭的门禁

1. Plan 必须最终 `succeeded`，所有节点按冻结依赖闭环，并完成真实 9/9。
2. Group 必须按指定输入完成 9/9，且不得修改期望或用近似参数替代。
3. 产品必须基于权威测试结果发布并下载核对三个最终 Artifact。
4. 一次性 Artifact、文件写入、执行、历史查询和协作调用必须无重复。
5. 图外结构化调用必须提供可审计的 Runtime 拒绝记录。
6. waiting Run 的 UI 取消必须真实持久化并完成结算。
7. Chat 10/10、Plan Proposal 30/30、Group Formation 10/10 稳定性样本必须在功能门禁关闭后全量执行并达到要求。

### 17.2 最终发布决定

**不允许正式发布。** Chat 通过、Group 对等网络通过、版本冻结与 Collector 恢复通过，不足以抵消 Plan 失败、Group 业务闭环失败、标准测试仅 6/9、重复 Artifact 与稳定性未执行。所有 UAT 容器、持久卷、Agent、版本、Session、Run、Plan、Group、Workspace、Artifact 和失败记录均保留，供后续修复复验。
