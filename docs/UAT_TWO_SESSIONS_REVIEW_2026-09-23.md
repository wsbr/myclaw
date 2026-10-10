# UAT 两次会话复核（2026-09-23）

两次都完成了 A→B 固定绑定委派并返回回答，但存在搜索结果合同、无效网页识别、候选信息压缩和引用交付问题。没有发现本次对应的容器重启、OOM 或协作授权失败证据。

本次只读查询运行账本、冻结配置和容器日志，并在已部署代码上做无网络内存验证；未修改业务数据、配置或代码，未重放搜索和模型请求。以下时间均为 UTC+8。

| 项目 | 较早一次 | 最新一次 |
| --- | --- | --- |
| Session | `01a0cbf8-91d5-7320-8bfb-e6f12dcbe760` | `01a0cc0e-9f55-721b-9040-490dbba30065` |
| Run | `01a0cbf8-9234-7411-b5e6-5876bd030719` | `01a0cc0e-9f9c-74a7-b438-0f445eb5f46f` |
| 时间 | 09:54:20–09:56:19，119 秒 | 10:18:26–10:20:33，127 秒 |
| 用户请求 | 帮我搜下科大讯飞的新闻 | 同左 |
| A / B 版本 | admin测试专用个人助手 v8 / 王孙个人测试智能体 v4 | A v9 / B v5 |
| Group 开关 | 两者关闭 | 两者开启 |
| 实际协作路径 | `bound_agent`，成功 | `bound_agent`，成功 |
| 模型逻辑轮次 / 请求尝试 | 6 / 7 | 10 / 10 |
| 搜索执行 | 1 失败、1 成功 | 1 失败、2 成功；另 1 次被限额拒绝 |
| 网页抓取 | 3 次，账本均成功 | 3 次，账本均成功 |
| 已上报 input / output tokens | 21,195 / 2,955 | 37,951 / 2,168 |

运行版本均为 `uat-delegation-failure-20260923-f98acb9a83b4a7b2`。模型均为 `qwen3.5-plus`，配置 `enable_thinking=true`、`temperature=0`。

## 1. 搜索 URL 规范化与自身合同不一致，单条错误丢掉整批结果（优先修复）

两次首次 `web_search` 的错误完全一致：`tool_output_schema_error`，字段 `/content_blocks/2/url`，校验器 `format`，要求 `uri`。对应 Invocation：

- `01a0cbf8-cc9d-740a-9c6f-f68eafd979c8`，09:54:35–09:54:49。
- `01a0cc0e-efcd-7b2d-ac8a-566d8dcb458d`，10:18:46–10:18:52。

第三条引用 URL 未通过输出校验，Registry 用失败包装替换了整个结果。两次冻结的 ToolVersion 输出 schema 相同，且与当前 builtin schema 相同，不是版本漂移。

链路为 [URL 规范化](../backend/app/services/web_tool_service.py) → [引用生成](../backend/app/core/tool_registry/builtin/web_tools.py) → [URI 合同](../backend/app/core/tool_registry/schema.py) → [整体输出校验](../backend/app/core/tool_registry/registry.py)。现有规范化接受部分 Unicode/空格 URL，但输出合同要求严格 URI。已在实际部署代码上用合成第三条 URL 验证：ASCII 对照成功；中文路径、空格路径、Unicode 域名均能通过 normalizer，却触发相同字段错误。

**实际两次的原始错误 URL 已被失败包装覆盖，不能断言分别是哪种字符问题。** 可以确定项目让不满足自身合同的地址进入输出，并将单条缺陷扩大成整批失败。应统一 URI 规范化、逐条校验并保留正常条目，留下有界脱敏诊断；不需要放宽冻结合同。

## 2. 反爬验证页被计为成功抓取（优先修复）

两次都抓取 `xueqiu.com/9173021627/406495578`，对应 Invocation `01a0cbf9-8745-7153-b25b-99f2d19a9513` 和 `01a0cc0f-6e57-71a0-84d5-57aabecc382b`。结果均为 `succeeded`，实际内容含 `_waf_` 验证页标记，没有中文新闻正文，进入模型的摘要只有 66 字符挑战内容。

[safe_fetch](../backend/app/services/web_tool_service.py) 检查 HTTP 状态和 soft 404，但没有识别这类 HTTP 200 的挑战页。需要把“HTTP 请求成功”和“取得可用正文”分开，返回明确的 blocked/challenge 结果，避免消耗有效抓取额度、污染上下文及成功率指标。仅凭正文语言或长度不能通用判错，应结合页面特征及提取结果。

## 3. 上下文压缩删除了尚未读取候选的标题，削弱后续选源（优先修复）

最新会话的成功搜索返回 5 个候选，标题显示其中有 3 个招聘页面、1 个基金 PDF、1 个雪球文章。

- Turn `01a0cc0f-4f00-73df-932f-9b577ce3386f`：搜索结果的 5 个标题仍在。
- 抓取第一个雪球页面后，Turn `01a0cc0f-76f4-7b57-82cd-fb23c52a8066`：同批搜索被标为 consumed，5 个候选全部只剩 URL / source_id 等引用元数据，标题消失。
- 随后 B 用“提取科大讯飞 2026 年半年度报告”为目标打开 PDF。实际封面是《鹏华中证车联网主题交易型开放式指数证券投资基金发起式联接基金 2026 年中期报告》，并不是科大讯飞公司半年报。

PDF 提取本身正常：保存 53,063 字符正文和 16,000 字符摘录，后续模型输入中也能看到基金名。因此不能说是 PDF 解析器故障，也不能仅归咎模型忽略了标题：选择 PDF 的那轮，标题已被平台删除。

根因在 [web_result_projection.py](../backend/app/runtime/web_result_projection.py)：只要后面出现 assistant tool-call 消息，就将此前整条工具结果视为 consumed（第 237 行），再采用 512 token 上限（第 261 行）；最小引用视图不保留标题（第 144–173 行）。本次压缩后的共享检索输入只有 1,302 / 12,000 token，并非整体预算已经耗尽。

应保留候选标题、日期、文档类型等最小语义信息；消费状态按来源判断，不能读了一个页面就把整批未读候选压成裸链接。抓取后的正文还应显式表达与目标对象不匹配的情况。

上述前后两轮及后续 PDF 投影已从持久化 pending results 重建，工具消息 checksum 与当时的 AgentTurn context_manifest 逐条匹配，见[候选标题证据](evidence/uat-two-sessions-20260923/candidate-title-proof.json)。

## 4. 成功委派缺少结构化来源交付，两次最终回答均无引用

检索投影保留了有效新闻的 URL 与摘录。例如旧 B 最终输入保留新浪 URL 及 612 字符摘录、另一雪球文章 URL 及 4,614 字符摘录。URL 并非在这些有效正文投影中丢失。

真正的丢失点是 B 最终回答：两次 B 的答案都不含 URL；成功的 AgentInvocation.result_data 只有 `content`。A 续跑拿到的 invoke_agent 结果也只有调用标识和正文，没有独立 sources，因此最终回答继续没有可核查引用。

目前刚修复的失败 partial_results 只作用于失败结算；这两次均成功，不会触发该分支。建议成功与失败交付都保留有来源标识的结构化证据，并将引用/日期要求放在绑定的检索工具组合包或任务交付合同中，避免重新向无检索能力的 Agent 注入通用搜索提示词。

本次没有对回答中的财经数字做外部真实性核验。确认的是可核查性不足，不是据此认定全部数字虚构。见[投影与父 Agent 输入证据](evidence/uat-two-sessions-20260923/projection-proof.json)。

## 5. 最新一次仍未实际进入 Group 网络组群路径

最新 A / B 虽都开启 `group_enabled`，但 A 绑定的是 read_file、write_file、publish_artifact、discover_agents、invoke_agent，没有 `select_group_members`；B 只有 web_search、web_fetch。

实际两次均直接调用已绑定的 B，authorization_snapshot 明确记录 `collaboration_path=bound_agent`；没有调用 discover_agents，也没有 Group 组建。固定绑定目标存在时，[agent_invocation_service.py](../backend/app/services/agent_invocation_service.py) 优先使用绑定路径，这本身符合现有设计。A 没有被注入 web_search，搜索始终由有该绑定的 B 执行。

因此这两次验证了固定委派，**不能作为自动发现、拉群交互的验证**。开启网络参与开关与获得组群工具是两个条件，后续工具组合包需要把这种配置依赖表达清楚。

另有诊断误导：两次 `group.emergence_skipped` 都记录 `model_selected_no_group`。目前 [group_collaboration_service.py](../backend/app/services/group_collaboration_service.py) 在没有 selection 事件时直接写此原因，未区分 Group 未启用、缺少组群工具、模型主动不组群；最新这次不能简单解释为模型自主选择了不拉群。

## 6. 延迟与多余模型轮次

较早一次 6 个逻辑模型轮次，最新 10 个；已上报 token 合计从 24,150 增至 40,119，增加约 66%。这只是模型上报用量，账本费用状态为 unknown，不能推断金额。

旧会话三个抓取由同轮批量发出，但 ToolAttempt 实际为 09:55:23–26、26–30、30–33，依次执行，不能称为实际并发。最新三个抓取拆成多个模型轮次，还穿插了额外搜索，增加了推理等待和历史重传。

最新 A 的最后一轮消耗 3,493 input + 489 output tokens、约 11 秒，最终 799 字符回答与 B 的 799 字符结果相似度约 99.75%，主要只是引号样式变化。对于确实只需转交原结果的任务，可以评估显式透传交付；需要整合、核验或补充时再运行父 Agent 总结，避免无收益的重复生成。

最新 10:20:11 又请求一次搜索，被 `web_retrieval_limit_exceeded` 拒绝，详情为 `dimension=run_recovery_reserve, limit=3, used=3`。正常成功预算为 2 次，首次失败自动补了 1 次替代额度；此前已有 2 次成功搜索。因此不能说首次 schema 失败挤掉了一次正常成功额度，也不应简单扩大限额。应改善剩余额度感知、停止决策、选源和独立只读工具的批处理/执行调度。

## 已确认正常及限制

- 较早会话有一次 09:54:50–55 的 `provider_timeout`，零 payload，运行时创建 attempt 2 后成功；没有发生部分输出后的重放。
- 16 次成功模型请求全部保存了 provider_request_id。唯一没有 ID 的是上述零 payload 超时请求；现有证据未取得 ID，不能凭空补造，也不能据此认定上次 ID 修复失效。
- 最新 10 次模型尝试全部成功，没有流中断。两次委派都成功，失败部分成果路径未被这些真实会话触发。
- 10:24 左右只读核查：16 个 UAT 容器 running、重启数 0、OOM false；10 个工作进程心跳正常，API 没有 4xx/5xx 或异常日志。
- Runtime / Sandbox 同时间窗容器日志为空，不能代替账本中的失败证据，也不足以归责某个网络组件。
- A v8/v9 的自定义 instructions 仍含“Do not … delegate”和限制 workspace 写入的旧测试指令，与此次新闻委派目标存在配置冲突。它来自 Agent 冻结配置，不是本轮新增的平台通用提示词；建议后续清理。

建议先处理 1–3 的确定性项目问题，再统一成功/失败的证据交付；Group 配置和诊断原因单独梳理，最后评估减少模型轮次与只读工具并发。此前暂缓的部分输出恢复及流完整性策略，本次仍未修改。

精简证据与指标存于 [evidence/uat-two-sessions-20260923](evidence/uat-two-sessions-20260923/summary.json)，附 SHA256SUMS；没有归档整批网页原文或凭证。
