# vibe_3 controlled-UAT 候选封板审计（2026-08-10）

## 结论

本轮把上一版审计中的 P1 2–5 收敛为 Runtime 不变量与可重复门禁，并把此前同属 `vibe_3` 的 Context/Memory/Skill、委派 authority、预算和管理页面改动一起冻结。它是受控 UAT 候选，不是企业生产发布声明。

```text
controlled_uat_candidate: GO（以包含本文的 clean SHA 门禁为准）
phase_one_definition_of_done: NO-GO
enterprise_production: NO-GO
full_search_real_gate: GO（Bailian 原生 Search 模式；以本文 clean SHA 证据为准）
```

封板提交不在正文自引用 SHA。`test/scripts/run_ci_gates.sh` 会要求工作树 clean、HEAD 与 `MYCLAW_ACCEPTANCE_COMMIT_SHA` 完全一致，并输出绑定该 SHA 的 JUnit 与 JSON manifest；浏览器证据另输出 PNG checksum manifest。

## 本轮关闭项

### Provider 不完整终止

- 只有 `END_TURN`，或兼容 Provider 的 `UNKNOWN` 加有效 Assistant Message，才能完成无 Tool 的 Turn。
- `MAX_TOKENS`、`CONTENT_FILTER`、`ERROR` 与无 Tool 的异常 `TOOL_USE` 显式失败；带截断 stop 的 Tool 片段也不调度。
- 错误码固定为 `provider_output_truncated`、`provider_content_filtered`、`provider_invalid_stop_reason`。
- `AgentTurn` 与 `ProviderAttempt` 保留私有 Items、stop reason 和 Provider 返回的真实 usage；部分回答不创建 committed Message，也不自动续写。

### WorkingPlan 终态不变量

- 除只读 Plan 提案阶段外，Chat、批准后的 Plan、Group 成员、Reporter 和被委派 Agent 在提交 final 前都检查当前 Agent 的最新 PlanUpdate。
- 存在 `pending` / `in_progress` / 非法 item 时，Runtime 写入 `working_plan.completion_required`，拒绝该次 final，在同一 WorkItem 上递增 epoch 并创建 replacement Runner。
- replacement 继承已有 continuation，仅保留一条去重的 System Runtime notice；它继续使用原 Run 的累计真实 usage 预算。
- 全部 item 完成后才允许发布最终消息；完成的 Agent WorkingPlan 随对应 WorkItem 关闭。
- Group 初始 Reporter 会收到冻结成员的精确 Agent ID，并被明确要求通过 `invoke_agent` 取得同伴贡献；消息正文里的 `@`、名字或请求不产生 AgentInvocation，也不计为真实委派。

### History / Memory maintenance

- retention leader 与 `summarize_history`、`extract_memory` 消费循环并发运行；命令使用 `SKIP LOCKED`、lease token fencing、heartbeat、指数退避与 dead-letter。
- 两类任务固定使用原 Runner / History 选择时冻结的 ModelVersion、PolicyVersion 与 Secret reference，不回写已结束 Run 的硬预算。
- History 重新计算可见消息范围和 source hash，成功写 `history.summary_ready`，终态失败写 `history.summary_failed`；失败不回滚原 Run。
- Memory 模型只看到当前 Run 中可见的 committed 用户消息及临时 `uN` ref。平台验证 ref、逐字 evidence quote、固定 policy scope、`confidence >= 0.8` 与最多 10 条，再进入既有 candidate arbitration；用户编辑继续优先。
- maintenance 事件只记录 model version、真实 usage、候选统计和安全错误码，不记录凭据或 Prompt 正文。

### Context 80% 软水位

- `hard_input_limit = context_window - frozen max_tokens`，soft target 为其 80%。
- 计数对象是完整请求：Messages、Tool schema、WorkingPlan、Skill catalog、策略段及未消费结果。
- Anthropic 使用原生 count API；官方 OpenAI 使用模型 tokenizer；其他 OpenAI-compatible 使用保守 UTF-8 / JSON 结构估算。计数失败降级并记录方法。
- 超过 soft target 时只从最旧 ambient History 开始裁剪；ready 语义摘要已作为低 authority 数据段先行替代其历史前缀。旧的 hash fact System 伪摘要已删除。
- System / Agent 指令、当前输入、WorkingPlan、Skill / Policy 段及 pending Tool / Agent 结果不裁剪。不可裁剪内容可在 hard limit 内超过 soft target并产生告警；超过 hard limit 才以 `context_window_exceeded` 拒绝。
- Run 累计 Token 预算仍仅依据 Provider 返回的真实 usage 做调用后审核：剩余 1 Token 时允许一次完整调用，下一次调用才拦截。

### Bailian 原生 Search Provider

- 新增 `bailian` Search connection，使用 write-only Bearer Secret 和不可变 connection revision；Bocha / Brave 兼容路径保持不变。
- Adapter 调用百炼原生 Generation API 的 `enable_search` + `forced_search`，只采信 `output.search_info.search_results`；模型生成正文被丢弃，缺少结构化搜索证据时以 `provider_search_not_executed` 显式失败。
- `pw` / `pm` / `py` 按百炼 `turbo` 的公开合同映射为 7 / 30 / 365；该接口不接受数值 1，因此 `pd` 保留为平台审计参数并把“过去 24 小时”确定性注入检索 query，不向 Provider 发送非法 freshness。country filter 仍显式拒绝，不静默忽略。
- 搜索调用记录安全的 request id、模型、策略、延迟和真实 token usage，不记录 Prompt 正文或凭据；生成正文固定限制为 64 tokens 以控制 Tool 成本。
- 管理页面可直接新建、加密保存、设为主搜索并真实测试 Bailian；Agent 发布时继续冻结主 Search connection revision。

## 证据边界

封板后的必跑证据分三层：

1. `standard`：全部 Backend unit、Frontend production build、空 MySQL migration、单一 Alembic head、MySQL Tool / Skill integration 与 schema/trigger contract。
2. `minimal-real`：只要求真实 LLM，不要求 Search provider；覆盖多步骤 Chat 最终 Plan 全完成、长 Session 异步语义摘要并在下一 Runner 使用、明确用户事实异步提取与召回、Skill 上传/发布/同步及模型渐进读取、Chromium 页面查看/编辑/预览并保存不可变新版本。
3. `real`：要求真实 LLM 与显式选择的真实 Search 模式。本候选使用 `MYCLAW_REAL_SEARCH_PROVIDER=bailian`，覆盖 Bailian 中英文和四种 freshness、Search→Fetch→引用、动态 MCP、隔离 Chromium、30 项自然语言盲测、多步 Chat / Plan / Group 及真实产品 UI。

当前 real/minimal-real 模型为 `qwen3.5-plus`。fixture 按百炼公开规格冻结 1,000,000 token context window；其他模型默认保持保守 32,768，亦可通过 `MYCLAW_REAL_LLM_CAPABILITIES` 显式覆盖。不得为了通过门禁把真实 1M 模型误冻结为旧 32K，也不得绕过 Runtime 的 hard-limit 检查。

`minimal-real` 输出：

- `minimal-real-fullstack.xml`；
- `minimal-real-gates.json`（绑定 commit SHA）；
- `minimal-browser-checksums.txt` 及其 manifest SHA-256；
- Skill 页面浏览器截图。

`real` 另输出 `real-model-fullstack.xml`、`real-gates.json`、`real-browser-checksums.txt` 及其 manifest SHA-256，并绑定同一 clean commit。Bocha / Brave 双 Provider 与 failover 矩阵仍作为扩展验收项，不再阻断本次 Bailian 真实 Search 完成口径。

## 回归覆盖

本轮新增或强化的确定性覆盖包括：

- 三种异常 Provider stop、异常 Tool stop 与部分输出不发布；
- incomplete WorkingPlan replacement、notice 去重与完成后才发布；
- maintenance claim、heartbeat、stale-token fencing、retry、dead-letter、History ready/failed usage 事件；
- Memory 用户来源校验与既有 user-edit-wins arbitration；
- 80% History 裁剪、Tool schema 计数、计数降级、硬超窗生成前拒绝；
- “Run 预算只剩 1 Token 仍调用，真实 overage 只拦下一次”的回归。
- Bailian 强制搜索请求、四种 freshness、真实 usage、安全配置边界，以及“HTTP 200 但没有 search_info”不可发布为证据。
- Group Reporter 使用 `invoke_agent` 对两个冻结成员完成真实委派，以及委派结果保持 Tool continuation authority、不进入 System role。

## 保留风险

- 本候选只证明 Bailian 原生 Search 的真实可用性；Bocha / Brave 双 Provider 可用性与跨 Provider failover 未由本候选证明。
- Bailian Search 内部仍会产生模型 token 和搜索费用；连接 config 冻结具体模型/策略，但生产前仍需独立成本上限、配额告警与供应商 SLO。
- maintenance 模型调用不计入已终态 Run 的硬预算，但仍会产生独立 Provider 成本；生产前需要单独的 maintenance 成本/SLO 配额。
- soft target 不是硬拒绝线；当不可裁剪 authoritative input 较大时会允许进入 80%–100% 区间并告警。
- 当前部署完成口径仍是单节点受控 UAT；HA、RPO/RTO、容量、红队和长时稳定性证据未满足企业生产门槛。

## 封板操作约束

- 将 `vibe_3` 当前全部改动作为单次提交：`chore: seal vibe_3 controlled-UAT candidate`。
- 提交前跑 unit、compile、diff、Frontend build、MySQL migration/integration 与定向测试。
- 提交后在 clean SHA 上从头跑 `standard`、`minimal-real`、Bailian `real`，随后用同一 SHA 重建本地 UAT并检查健康状态。
- 任一失败均修复后 amend 同一封板提交，并从 clean-SHA gate 起点重跑。
- 最终工作树必须 clean、Alembic 必须单一 `0033` head；本轮不 push、不打 tag。
