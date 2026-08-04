# 小型软件项目开发组三模式协作 UAT 证据索引

本索引对应 [MyClaw 小型软件项目开发组三模式协作 UAT 报告](../../phase-one-dev-team-uat-2026-08-04.md)。最终只读快照时间为 `2026-08-03T19:08:41Z`（UAT 日期 2026-08-04，Asia/Taipei）。

证据只包含脱敏标识符、状态、统计和 checksum；不包含密码、模型密钥、Token、Credential 明文或 Authorization Header。原始 UAT 容器、数据库事件、Trace、Workspace、Artifact 和失败 Run 均保留在 `myclaw-uat` 持久环境中，没有复制项目文件到宿主机作为替代执行证据。

## 1. 结论索引

| 验收项 | 结果 | 主证据 |
| --- | --- | --- |
| Chat | 通过 | Run `019fc8df-60ce-7c58-9cc4-bc66183309a0` completed/succeeded；同一 WorkItem epoch 1→4；Artifact checksum `866d22e...` |
| Plan | 不通过 | Run `019fc8e3-08b5-7e72-8ef0-123b29794dd1` waiting/tool_confirmation；6 WorkItem 仅 2 succeeded |
| Group 对等网络 | 通过 | 5 条真实组内 AgentInvocation，产品、开发、测试双向关系齐全；45 Span Link |
| 真实代码 | 存在 | Workspace `capacity_risk.py` 2095 bytes，checksum `2c07b573...` |
| 标准 9/9 | 不通过 | 精确 argv 仅覆盖 T02、T03、T04、T05、T06、T09，即 6/9 |
| 安全与恢复 | 不通过 | 角色隔离及 Worker 恢复主体通过；重复 Artifact checksum `aa07f713...`，图外拒绝缺少 AgentInvocation 审计记录 |
| 稳定性 | 未执行／不通过 | 功能门禁未满足，Chat 10、Plan 30、Group 10 未启动 |
| 正式发布 | 不允许 | Plan、Group、9/9、安全一次性和稳定性门禁未关闭 |

## 2. 环境与备份证据

| 项目 | 证据 |
| --- | --- |
| Compose | `myclaw-uat` |
| UI / API | `localhost:8181` / `localhost:8101` |
| MySQL / Redis / OTLP | `127.0.0.1:3310` / `127.0.0.1:6390` / `127.0.0.1:4320` |
| Git HEAD | `c93e270a6ae7a3ad953eb26087019036c9a0f6c1` |
| 真实模型 | `uat-primary-qwen`，Model ID `019fc637-af5d-7874-9f26-23c324a2a48d`，`qwen3.5-plus` |
| 备份文件 | `deploy/uat/backups/myclaw-uat-20260803T181517Z.sql.gz`，322039 bytes，mode 0600 |
| 备份 SHA-256 | `774d5a776981e1932cd660335bdb398c3855a7dcb0b99e4b11b5d009f8820c98` |
| 备份验证 | `gzip -t` 与重新计算 checksum 均通过 |

## 3. AgentVersion 证据

| 角色 | Agent ID | 冻结／发布版本 |
| --- | --- | --- |
| 个人助手 | `019fc8d9-f46c-77e8-9c47-853170d0c61b` | v1 `019fc8da-2943-7c1d-bc96-8a0b09ba1327` |
| 产品 | `019fc8db-304b-72ec-95f0-8f93347f461a` | v1 `019fc8db-3c55-7999-bb71-783a91b5ef96` |
| 开发 | `019fc8db-ddf9-76fa-9b19-8bde69242bdb` | v1 `019fc8db-ea00-73ee-8e64-ffddeb015e2a`；v2 `019fc8e6-550d-7c4a-806b-6953ad176600` |
| 测试 | `019fc8dc-4bc5-7be2-8413-18b669ddaaaf` | v1 `019fc8dc-57dd-7363-8b7e-966ce26267ce` |
| 项目经理 | `019fc8dd-619c-7ddb-b345-1921bea5e3bb` | v1 `019fc8dd-6da7-7341-8bd8-f53d94ff9851` |
| 图外顾问 | `019fc8dc-a1e2-7eec-9768-df9e222ffcc4` | v1 `019fc8dc-adf3-7359-96fc-61c586b3e6f5` |

全部使用 `context.standard`、`history.summary_recent`、`memory_write.user_agent_facts`、`memory_recall.user_combined`、`local` Sandbox。Plan 冻结开发 v1 后发布 v2，Plan 仍引用 v1；Group 冻结开发 v2，实际组内调用均引用 v2。

## 4. Chat 证据账本

| 类型 | 标识符／结果 |
| --- | --- |
| Session | `019fc8de-88a8-7884-aa92-5d254217a129` |
| Run | `019fc8df-60ce-7c58-9cc4-bc66183309a0`，completed/succeeded |
| WorkItem | `019fc8df-60ce-7725-86c7-46c99773ce3e`，epoch 1→4，succeeded |
| 最终 Runner | `019fc8e1-3817-7451-9c00-ea5d724e414f` |
| ToolInvocation | 2 succeeded：文件写入一次、Artifact 发布一次 |
| AgentInvocation / execute | 0 / 0 |
| Artifact | `019fc8e1-3999-7668-9747-a01b51afdd9a`，`Sprint容量计算器-行动清单.md`，1341 bytes |
| Artifact SHA-256 | `866d22e9795a5e99606e01260cda957480f294737cfd884d90f446479705b055` |
| Completion | clarification/write/publish 均 satisfied，pending `[]` |
| 下载 | API access log GET 200；正文与 Workspace 文件一致 |

## 5. Plan 证据账本

| 类型 | 标识符／结果 |
| --- | --- |
| Session | `019fc8e3-0890-7451-a436-590fa2a1d490` |
| Run | `019fc8e3-08b5-7e72-8ef0-123b29794dd1`，waiting/tool_confirmation |
| Plan | `019fc8e3-08b5-7a46-baa7-2bf04af8d117` |
| Proposal Runner | `019fc8e3-08b5-77e2-bba5-b6f6542ecd18`，`structured.plan_proposal.v1`，repair 0 |
| Frozen revision/hash | revision 2，`0deb06ba01fcf002fc44495eb0f2bca5f446d66d71f89e257cdbd3b061099562` |
| Frozen versions | 产品 v1、开发 v1、测试 v1、项目经理 v1 |
| WorkItem | 6：2 succeeded、2 waiting_agent、2 waiting_user |
| ToolInvocation | 29：5 succeeded、14 failed、6 rejected、2 waiting_resource、2 waiting_confirmation |
| AgentInvocation | 2 running |
| Artifact | 0 |
| UI cancellation | 两次动作后 `cancellation_requested_at` 仍为空 |

冻结 DAG：`PM_INIT→PROD_FREEZE`、`PM_INIT→TEST_DESIGN`、`PROD_FREEZE→DEV_IMPL`、`TEST_DESIGN→TEST_EXEC`、`DEV_IMPL→TEST_EXEC`、`TEST_EXEC→PM_SUMMARY`。

## 6. Group 证据账本

| 类型 | 标识符／结果 |
| --- | --- |
| Parent Session | `019fc8eb-0bf0-7216-ae0b-5d464bc80dc1` |
| Formation Run | `019fc8eb-0c1e-796e-af9c-9db1fcb0e6b1` |
| Formation Proposal | `019fc8eb-0c1e-7a62-9245-30c41b91d31e`，revision `019fc8ec-110d-7aac-9a6a-23fae1818a6d`，hash `6669a7a9eafc2e8498855def1270f3ef441d775843d6204b8fa81b1fd6a8007f` |
| GroupSession | `019fc8eb-0c1e-7bc6-8134-d52cd4eeac1e` |
| GroupRun | `019fc8ec-e193-7c94-a1a7-d1463993b075` |
| Run | `019fc8ec-e193-7e8f-8c47-a9e36672a657`，waiting/tool_confirmation |
| Frozen nodes | 产品 v1、开发 v2、测试 v1；外部顾问不在图内 |
| WorkItem | 13：5 succeeded、4 failed、1 waiting_agent、3 waiting_user |
| ToolInvocation | 128：98 succeeded、6 failed、18 rejected、1 waiting_resource、5 waiting_confirmation |
| AgentInvocation | 5：4 succeeded、1 running |
| Artifact | 4 个需求类文件；最终三个交付物均缺失 |

### 6.1 对等协作关系

| source → target | AgentInvocation | 状态 |
| --- | --- | --- |
| 开发 → 产品 | `019fc8ef-b489-716e-aa66-8c9898446444` | succeeded |
| 测试 → 产品 | `019fc8ef-dacf-7850-b243-5d0052d5f870` | succeeded |
| 测试 → 开发 | `019fc8ef-dae5-747b-9320-f839110f7691` | succeeded |
| 产品 → 开发 | `019fc8f0-508f-78b8-add7-fd12dd27d896` | succeeded |
| 开发 → 测试 | `019fc8f7-5c6e-7679-8183-7541a07597fa` | running |

### 6.2 程序与测试

| 项目 | 证据 |
| --- | --- |
| 程序路径 | `/var/lib/myclaw/workspaces/019fc8eb-0bf0-7216-ae0b-5d464bc80dc1/capacity_risk.py` |
| 大小 / SHA-256 | 2095 bytes / `2c07b5731333bfa9a09fcb5eff8e1b561a68d1ed7f65803bf839c1af26dfdb5a` |
| 精确通过 | T02、T03、T04、T05、T06、T09 |
| 未精确执行 | T01 用 138/120 代替 120/138；T07 用 0/50 代替 0/10；T08 用 100/-5 代替 100/-1 |
| 结论 | 6/9，不是 9/9 |

### 6.3 Artifact 一次性证据

`019fc8f3-0b89-757a-8ee3-b1e99a0fe192` 与 `019fc8f8-862c-7f46-8c87-a070c2d12adb` 均为 2168 bytes，SHA-256 都是 `aa07f7139b46f2b9abb2ebb9384aba9b6e247aba3bc0236ed985eb5065ef7bdc`，但来自不同 WorkItem/Invocation；据此判定一次性发布失败。

## 7. Worker 与 Collector 证据

| 对象 | 重启证据 | 结果 |
| --- | --- | --- |
| Runtime Worker | container `29f3507...`，StartedAt `17:11:55Z→18:25:39Z`；Chat 同一 WorkItem epoch 1→4 | 状态恢复，无重复 Chat 副作用 |
| Sandbox Worker | container `1be8f0e...`，StartedAt `17:11:54Z→18:44:26Z`；重启前 3、后 54 ToolAttempt 成功 | 调用持续执行，无重复 idempotency key |
| Collector | container `70b427...`，StartedAt `17:27:36Z→19:00:07Z` | 健康恢复并继续写入 |
| Trace 文件 | 148 行/332432 bytes → 149 行/334484 bytes | 重启后追加成功 |
| 核心 Run 命中 | Chat 4、Plan 23、Group 99 | 三个核心 Run 均持久化 Span |
| 日志扫描 | permission denied=0、Exporting failed=0、敏感模式=0 | 通过 |

## 8. 安全探针

| 探针 | Session / WorkItem | 结果 |
| --- | --- | --- |
| 个人助手协作与执行 | `019fc8fb-444f-...` | Clarification 式拒绝；AgentInvocation=0，execute=0 |
| 产品执行 Python | `019fc8fb-c564-...` | 无 execute |
| 开发发布 Artifact | `019fc8fc-3dfa-...` | publish=0，Artifact=0 |
| 外部顾问无 Tool | `019fc8ff-a7ca-...` | ToolInvocation=0 |
| Group 图外调用 | `019fc8fd-db2c-7d6b-a40c-3a3ceb37d7e8` | capability policy 拒绝，无越权调用；缺少 AgentInvocation rejected 账本 |
| 文字冒充测试/发布 | Group 错误测试与产品报告 | 写入/发布被拒绝，Group 未成功 |

## 9. 证据边界与保留策略

- 原始自然语言、UI 操作与真实模型执行是产品 UAT 证据；只读 SQL/API/容器检查只用于对账 UI 未完整展示的 ID、epoch、状态、checksum、Trace 和冻结版本。
- 没有把技术断言、配置页面、模型文字或未执行的 waiting ToolInvocation 计作产品通过。
- 没有保存原始认证响应、请求 Header 或环境变量值。
- 没有执行稳定性批量，因为功能门禁已经失败；所有失败样本和 waiting 状态保留。
- 没有清理 `myclaw-uat` 的容器、卷、Agent、版本、Session、Run、Plan、Group、Workspace、Artifact 或 Trace。
