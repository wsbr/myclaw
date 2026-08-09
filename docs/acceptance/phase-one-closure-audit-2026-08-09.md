# MyClaw 一期当前实现封板审计（2026-08-09）

## 1. 封板结论

本次封板是**当前实现、文档和证据的事实冻结**，不是“一期完成”或“生产发布”声明。

```text
code_candidate: GO（可提交、可继续受控 UAT）
phase_one_definition_of_done: NO-GO
enterprise_production_v1: NO-GO / not supported
product_market_fit: unproven
```

候选基于 `c97821cc8df011b777914d9051301d4a3185c10a` 继续封板；封板引用是**包含本文的 Git 提交**。这样避免在提交正文中自引用一个会随正文变化的 SHA。提交后的标准门禁证据由 `test/scripts/run_ci_gates.sh standard` 生成并在外部保留，不能用本文替代。

当前实现已经形成可运行的 Agent-native 纵向主链：Agent/Version 装配、统一 Agent Loop、Tool 版本和治理、Run/WorkItem/Runner、等待与恢复、Workspace/Artifact、Plan、Group、Event/Checkpoint/Outbox 以及管理 UI 都有实质代码。最成熟的是 Tool 契约和持久执行骨架；最大的缺口集中在上下文/历史/记忆策略未接入、Skill 执行快照链不完整、终态一致性仍部分依赖模型自律，以及缺少绑定同一 clean SHA 的完整一期 UAT 样本。

因此允许的表述是：

- 代码候选适合继续 `uat_single_node` 受控验证；
- 核心架构方向和主要持久原语已落地；
- 一期 15 项 UAT 门禁尚未全部证明；
- 企业生产、产品价值和商业就绪均未证明。

不得表述为：

- “一期已验收完成”；
- “25/25 等于完整 UAT”；
- “设计合同已写完等于生产就绪”；
- “Run completed 等于交付已验证”。

## 2. 审计范围与方法

本次检查覆盖：

- 366 个候选 Git 文件（含本封板文档）；
- Backend、Frontend、31 个数据库迁移、测试脚本与测试用例；
- 产品、技术、模块、运维和历史验收文档；
- 当前 Git 状态、提交历史、迁移 head、运行时 OpenAPI 与目标 OpenAPI；
- 单元测试、分支覆盖率、Frontend production build、Python compile、Markdown 相对链接和一条真实模型全栈回归；
- 已生成但未纳入 Git 的标准门禁和 UAT 结果文件。

证据按以下强度解释：

1. **已证实**：当前代码检查或可重复测试直接覆盖该结论；
2. **部分证实**：主链已实现且有局部证据，但没有达到权威门禁的范围、次数或故障矩阵；
3. **未证实**：没有符合当前权威门禁且绑定 clean SHA 的证据；不等同于“一定未实现”。

历史 V1 报告只保留历史事实，不用于证明 Agent-native v2 完成。`docs/technical/openapi-v1.yaml` 是目标机器契约，不自动视为当前运行时 API 快照。

## 3. 项目规模与实现盘点

统计基于本次封板候选；行数包含空行和注释，只用于说明项目规模，不作为完成度指标。

| 区域 | 文件数 | 行数/数量 | 当前判断 |
| --- | ---: | ---: | --- |
| 全仓候选文件 | 366 | 81,937 行 | 已形成完整产品、技术、实现、测试和部署树 |
| Backend Python | 158 | 33,525 行 | 主体实现；其中 `backend/app` 125 文件、28,701 行 |
| Frontend TS/TSX | 49 | 4,996 行 | 管理面和 Session 三模式 UI 已具备 |
| 测试 Python/Shell | 39 | 5,360 行 | 单元/集成/真实模型/浏览器门禁均有入口，但覆盖和证据不完整 |
| Markdown 文档 | 45 | 9,763 行 | 目标设计较完整，本次补入当前封板页 |
| Alembic migrations | 31 | 单一 `0031` head | 迁移链线性 |
| SQLAlchemy models | 87 | — | Agent-native 领域对象较完整 |
| FastAPI runtime OpenAPI | 125 paths / 150 operations | — | 当前代码生成事实 |
| 目标 OpenAPI | 98 paths / 118 operations | — | 目标契约；与实现快照不是同一用途 |

当前 Git 历史共 89 个提交，审计基线为 `c97821c fix cold-session tool discoverability`。封板前工作树还包含两项同一问题域的已有改动：

- Session 页面把待审批/澄清操作固定到输入区上方，避免长消息滚动后看不到动作；
- 增加“报告 Artifact 发布后 WorkingPlan 必须全完成”的真实模型回归，并拆出只依赖真实 LLM、无需 Bocha/Brave 的最小验收夹具。

两项改动均纳入本封板候选；Frontend build 与最终真实模型回归均已通过。

## 4. 能力进度

### 4.1 已形成可用纵向链路

| 能力域 | 状态 | 已有事实 |
| --- | --- | --- |
| Agent Registry / Version | 已实现主链 | Draft、发布、不可变版本、Model/Policy/Tool/Skill 绑定和 AgentCard |
| 统一 Runtime | 已实现主链 | Chat/Plan/Group 共用 `AgentLoopCoordinator`，持久 Run/WorkItem/Runner、Turn、Attempt、Checkpoint、Outbox |
| Tool 系统 | 当前最成熟 | Registry reconciliation、版本 checksum、治理、连接修订、schema dispatch、确认、幂等、MySQL 不可变 trigger |
| Workspace / Sandbox / Artifact | 已实现主链 | 文件读写、execute、worker lease、Artifact 发布/下载与 provenance |
| Plan | 已实现主链 | `plan_readonly`、WorkingPlan revision、内容 hash、批准、原 Session 继续实施、过期批准取消 |
| Group / Agent collaboration | 已实现纵向链 | Agent Network 发现、Membership、AgentInvocation、Group message、Reporter summary 和图数据 |
| 可靠性与可观测性 | 已实现骨架 | deadline、budget、重试/unknown、fencing、ExecutionEvent、Trace span、verify_run |
| Frontend | 已实现主要操作面 | Chat/Plan/Group、WorkingPlan、RequiredAction、Artifact、管理页面；待操作现固定在 Composer 上方 |

### 4.2 部分实现或仍未闭环

| 能力域 | 状态 | 缺口 |
| --- | --- | --- |
| Context policy | 部分实现 | `assemble_standard_context` 已定义但 Runtime 没有调用；实际仍由 worker 直接读全量 committed Message 后交给 `ContextAssembler` |
| History policy | 部分实现 | `select_history` 与 summary enqueue 已定义，但主 Runtime 没有接入，maintenance 也未找到消费 `summarize_history` 的 worker |
| Memory lifecycle | 部分实现 | Tool 和数据对象存在；`recall_memories`、terminal follow-up 与 `extract_memory` 消费链未接入当前主链 |
| Skill runtime | 部分实现 | Resolver 能解析 `SkillVersion`，但没有创建文件读取所需的 `RunnerSkillSnapshot`；Runtime 注入的是 manifest JSON，不是完整 Skill 内容 |
| Context compaction | 原型级 | 以 `len/4` 估 Token；不计 provider tokenizer/tool schema，压缩只保留数量/hash/最近角色/Plan revision，没有语义摘要 |
| Production profile | 仅设计合同 | 当前只支持单机受控 UAT；HA、SLO、容量、RPO/RTO、安全和演练证据缺失 |
| 产品价值 | 未证明 | 尚无设计伙伴、盲业务验收、A/B/C/D 对照或单位经济证据 |

## 5. 一期 15 项 UAT 门禁统计

按 `docs/modules/phase-one-scope.md` 的 15 项门禁逐项评估：**已证实 2/15，部分证实 9/15，未证实 4/15**。这个统计衡量的是“验收证据完整度”，不是代码行完成百分比。

| # | 门禁摘要 | 状态 | 本次判断 |
| ---: | --- | --- | --- |
| 1 | 固定模型/clean SHA、三模式同一 Loop | 部分证实 | 代码共用 Loop；没有三模式完整 clean-SHA 批量证据 |
| 2 | Chat 按需 Plan、简单任务不强制 | 部分证实 | 当前真实多步骤回归通过；缺少复杂/简单统计样本 |
| 3 | Plan 批准前零副作用、批准后同 Session | 部分证实 | 代码和真实 E2E 用例存在；当前封板未执行完整 Plan 门禁批次 |
| 4 | Group 来自工作网、无模板赋权 | 部分证实 | 数据和服务链符合；完整 Group 证据不足 |
| 5 | 非常驻 Agent、委派/Group 共用调度 | 部分证实 | 代码路径成立；恢复和并发矩阵未完整证明 |
| 6 | 精确九例 9/9 | 未证实 | 当前没有绑定候选 SHA 的 9/9 产物 |
| 7 | 四 Artifact 内容/checksum/download | 未证实 | 单条 Artifact 回归通过，未达到四成果合同 |
| 8 | Tool 可按业务重复使用 | 已证实 | 单 Loop 不移除成功 Tool；单元和 provider conformance 有重复调用探针 |
| 9 | 无 finalize/required-tool/final contract | 已证实 | 当前活动主链没有这些机制，正常 assistant message 可结束 Turn |
| 10 | 文字不能伪造 verified 事实 | 部分证实 | Tool/Artifact 事实分离已实现；仍缺完整负向/红队证据 |
| 11 | 等待、取消、重启恢复 | 部分证实 | 原语和局部测试存在；未完成权威故障矩阵 |
| 12 | 拒绝/失败/unknown/迟到可审计 | 部分证实 | 模型与事件支持；没有完整验收矩阵 |
| 13 | 无重复副作用/终态复活/旧 lease 写入 | 部分证实 | DB/lease/fencing 防线较完整；全故障注入未通过 |
| 14 | Chat/Plan/Group 批量阈值 | 未证实 | 当前 25/25 是五类 Tool 选择样本，不是规定的三模式批次 |
| 15 | 环境/版本/Trace/Artifact 可复现对账 | 未证实 | 最新完整 real gate 未执行；当前缺 Bocha/Brave 凭据 |

## 6. 本次测试与证据

### 6.1 当前候选的直接检查

| 检查 | 结果 | 证据边界 |
| --- | --- | --- |
| Backend unit | 130/130 passed，6.12 秒 | 当前工作树；纯单元 |
| Backend branch coverage | 130/130 passed，17.81 秒，total 40% | 当前工作树；覆盖不足本身是风险 |
| Frontend production build | passed，5,034 modules | 包含 PendingAction 布局改动 |
| Python compileall | passed | `backend/app`、migrations、test |
| Alembic | 单一 `0031 (head)` | 迁移图线性 |
| Git diff check | passed | 无 whitespace error |
| Markdown relative links | 初查 1 个断链 | 本封板已用当前文档替换失效链接 |
| 真实模型终态回归 | 1 passed，138.78 秒 | Qwen `qwen3.5-plus`；LLM + MySQL/Redis/worker/sandbox + 公网 fetch，不含 Search provider |

真实模型终态回归验证：

- `write_file`、`execute`、`web_fetch`、`publish_artifact` 均成功；
- Artifact 已发布；
- WorkingPlan 为 `closed` 且所有 item 为 `completed`；
- `artifact.published` 之后存在全完成的 `working_plan.updated` 事件。

该回归的准备过程还暴露两点测试质量问题并已修正：原提示会合理触发 clarification、测试却不会回答；原提示只要求“写报告”、断言却要求发布 Artifact。修正后的测试明确给出完整任务并要求发布为可下载成果。

### 6.2 已有 clean-SHA 证据

`test/test-results/standard-gates.json` 记录基线 `c97821cc8df011b777914d9051301d4a3185c10a` 在 2026-08-08 通过 standard gate。该结果不自动覆盖封板提交中新加入的 UI、测试和文档，因此封板提交后仍需在 clean SHA 上重跑 standard gate。

`current-uat-real-blind-25-fixed.json` 同样绑定该基线，记录 Qwen `qwen3.5-plus` 的 25/25：

- `list_files` 5；
- `search_files` 5；
- `web_fetch` 5；
- `browser_navigate` 5；
- `memory_search` 5。

该文件明确排除了 5 个 provider-pending case。它证明有限的自然语言 Tool 选择，不证明精确九例、四 Artifact、Plan/Group 批次、故障矩阵或完整一期 UAT。

### 6.3 没有通过的门禁

完整 `real` gate 未执行。`run_fullstack.sh` 和 UAT acceptance 脚本会强制要求：

- 真实 LLM key/endpoint/model；
- Bocha key；
- Brave key。

当前本地只有真实 LLM 配置，Bocha/Brave 均未配置。不得以假 key、Mock 或跳过连接测试替代。

同一模型配置在本次回归准备期间还出现一次 conformance 波动：`tool_result_to_plain_final_tool_still_visible` 探针一次失败，原样下一次通过。这说明当前 provider/model conformance 不是确定性绿灯，正式 UAT 应保留单次失败、统计 first-attempt/final success，并在发布前确定重试和准入策略。

## 7. 未关闭问题

以下严重度以“一期受控 UAT 发布”衡量；生产还有更多明确阻断项。

### P1：一期封板阻断

1. **四类上下文/历史/记忆策略没有接入 Runtime 主链**
   `context_service.assemble_standard_context`、`history_service.select_history`、`memory_service.recall_memories`、`run_followup_service.enqueue_run_terminal_followups` 只有定义，没有运行时调用点。文档宣称的策略冻结与真实 prompt 装配不一致。

2. **Context token 与 compaction 不足以支撑长会话**
   `ContextAssembler` 使用字符数除以 4 估算，不计 provider tokenizer 和 Tool schema；compact 结果不是语义摘要，而且 fixed/pending section 过大时仍可能越过预算。

3. **Agent 委派结果存在 authority 提升风险**
   持久历史中的 `tool_notice` 在 `runtime_worker.py` 被转换为 `system` role；其正文又包含目标 Agent 返回文本。即使已有“外部内容不可信”规则，这仍把数据放进了更高权重通道，应改为结构化 Tool result 或明确的低权重 untrusted data section。

4. **不完整 provider stop 可能被当作正常完成**
   `AgentLoopCoordinator` 先判断 `assistant_messages` 并返回 completed，之后才检查 `MAX_TOKENS` / `CONTENT_FILTER`。Provider 返回部分文本加截断 stop 时可能被提交为成功 final。

5. **Skill 执行快照链不完整**
   `SnapshotResolver` 解析 `SkillVersion`，但没有找到创建 `RunnerSkillSnapshot` 的路径；`skill://` 文件读取却依赖该对象。Runtime 当前只把 manifest JSON 注入 prompt，不能证明 Skill 包内容被精确版本加载。

6. **maintenance 命令缺少消费者**
   代码能 enqueue `summarize_history`、`extract_memory`、`sync_skill`，但未找到处理这些 action 的 worker。长期历史压缩、记忆提取和 Skill 同步无法据此宣称闭环。

7. **WorkingPlan 终态完整性仍依赖模型自律**
   当前真实模型回归通过，但 `_settle_completed` 只是把 active plan 改为 `closed`，没有验证最新 PlanUpdate 是否全部完成。已观察过 completed Run 留下 4/5 plan；现有回归降低复发概率，却没有把 invariant 固化在 Runtime。

8. **完整一期 UAT 证据缺失，provider conformance 有波动**
   缺少 clean-SHA 三模式批次、精确九例、四 Artifact、故障/恢复/观测证据；本次还观察到一次 conformance first-attempt failure。

### P2：应在下一轮收敛

1. **目标 OpenAPI 与运行时 API 不同**
   目标文件为 98 paths / 118 operations，运行时代码为 125 / 150。文档已经把前者定义为目标合同，因此不是简单“生成文件过期”；但发布资产需要额外生成 implementation snapshot 或差异清单，避免使用者误读。

2. **关键 Runtime 覆盖率过低**
   总 branch coverage 40%；`action_dispatcher` 11%、`agent_loop_coordinator` 15%、`context_assembler` 24%、`snapshot_resolver` 19%、`runtime_worker` 23%、Plan API 11%。当前测试数量不能支撑“无 P1”。

3. **真实模型 E2E 夹具此前耦合所有外部连接**
   只验证 LLM + 内置 Tool 的回归也被 Bocha/Brave 阻断。本封板增加了最小 `real_llm_world`，但完整 gate 仍保持严格三凭据合同。

## 8. 下一阶段建议顺序

1. 先关闭 P1 authority/terminal 问题：stop reason 顺序、Agent 结果 role、WorkingPlan terminal invariant；补确定性单元/集成测试。
2. 把 Context/History/Memory 四策略真正接入 Runtime，并实现 maintenance consumer；用长会话、预算上限和恢复测试验证。
3. 完成 Skill package materialization 与 `RunnerSkillSnapshot`，验证 checksum、重启和越权读取。
4. 把关键 Runtime 分支覆盖率提升到能覆盖等待、恢复、unknown、late、duplicate、fencing 和 terminal conflict，而不是追求无差别总百分比。
5. 配齐 Bocha/Brave 后，在封板 clean SHA 上先跑单次完整功能流，再跑权威批次；保留 first-attempt failure，不只保留最终绿灯。
6. 一期 UAT 全部通过后，再进入产品 A/B/C/D、设计伙伴和生产就绪工作；不要并行宣称生产或商业完成。

## 9. 封板清单

- [x] 盘点代码、文档、测试、迁移和 API 规模；
- [x] 复核统一 Loop、Plan、Group、Tool、Context/Memory/Skill 和终态链路；
- [x] 运行单元、覆盖率、Frontend build、compile、migration、diff 和链接检查；
- [x] 增加并跑通真实模型 WorkingPlan/Artifact 终态回归；
- [x] 修复验收索引中的失效链接；
- [x] 明确 GO/NO-GO 和未关闭 P1/P2；
- [ ] 在包含本文的 clean SHA 上重跑 standard gate；
- [ ] 配齐两个 Search provider 后执行完整 real gate；
- [ ] 满足 15/15 一期 UAT 门禁；
- [ ] 满足企业生产和产品价值 Gate。

封板提交完成后，第一项未勾选项应立即执行；其结果是封板提交的外部 gate evidence。后三项是后续里程碑，不属于本次提交的完成条件。
