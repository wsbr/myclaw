# UAT 会话 01a0c8a0 协作失败诊断

本次为只读诊断，没有重放用户请求、调用外部模型或修改 UAT 数据与代码。

## 结论

固定绑定委托已通过授权并成功运行。直接失败点是子 Agent 搜索、抓取之后的模型流式请求：HTTP chunked 响应尚未完整接收，对端关闭了连接。当前只对尚未收到有效输出的模型请求自动重试；该请求已有输出，因此被结算为委托失败。

**归因边界：这是已确认的失败触发点，不是“模型提供商有故障、项目没有问题”的证明。断流发生在哪一段链路尚未定位；项目自身的恢复策略和部分成果交付缺口，决定了这次断流最终变成用户看到的整个委托失败。**

- Session：`01a0c8a0-e4b6-7c2e-bee2-300ea706bb86`
- Run：`01a0c8a0-e4f4-72e6-bc9e-6127fbfce643`
- AgentInvocation：`01a0c8a1-28a2-77fd-9e15-811dfe61e50e`
- 主 Agent：`admin测试专用个人助手` v8；子 Agent：`王孙个人测试智能体` v4。
- 冻结配置中双方 `group_enabled=false`，授权快照明确为 `collaboration_path=bound_agent`，本次没有 GroupRun。
- 主 Agent 有 `read_file/write_file/publish_artifact/invoke_agent`，子 Agent 有 `web_search/web_fetch`。主 Agent 使用自己的委托能力，由子 Agent 使用自己的搜索能力。
- 调用模型：百炼兼容接口 `dashscope.aliyuncs.com`，`qwen3.5-plus`。

## 运行时间线（UTC+8）

| 时间 | 事实 |
| --- | --- |
| 18:19:43 | 用户发送“帮我搜下科大讯飞的新闻” |
| 18:20:00 | 主 Agent 成功调用 `invoke_agent`，创建子任务并等待 |
| 18:20:11–18:21:10 | 子 Agent 首次 `web_search` 遇到 `ConnectError` |
| 18:21:26–18:21:39 | 子 Agent 再次调用 `web_search` 成功，返回 3 条结果 |
| 18:22:13–18:22:24 | 3 次 `web_fetch` 工具均记录为成功 |
| 18:22:24 | 子 Agent 发起第 4 次模型请求，以处理抓取结果 |
| 18:22:29 | 模型请求收到有效输出 payload |
| 18:23:48 | 响应流中断，子委托被标记失败，主 Agent 被正常唤醒 |
| 18:24:28 | 主 Agent 返回失败说明，Run 结束 |

“web_fetch 成功”是工具账本状态，不等于已验证抓到完整新闻正文；例如雪球响应包含反爬内容。这是额外的抓取质量问题，现有证据不足以把它认定为流中断的原因。

## 直接故障证据

失败 Runner：`01a0c8a3-5b7b-7b59-822c-9571503643eb`。

失败 ProviderAttempt：`01a0c8a3-5cba-7afd-a716-f2169afbb891`。

```text
error_code = provider_transport_error
error_detail = peer closed connection without sending complete message body (incomplete chunked read)
status = outcome_unknown
received_payload = true
tool_call_observed = false
side_effect_committed = false
```

该原文对应已部署 h11 `ChunkedReader.read_eof()`：HTTP/1.1 chunked 响应体未接收完就收到 EOF。可以确认响应流提前断开；现有记录无法进一步判断由模型服务、代理网关还是中间链路关闭。

`tool_call_observed=false` 不足以证明是纯文字输出：未组装完毕的工具增量也可能已将 `received_payload` 置为真。

## 为什么没有恢复

`backend/app/runtime/model_adapter.py:232` 的自动重试条件要求 `not received_payload`。本次已收到 payload，未进入自动重试。

`backend/app/workers/runtime_worker.py:2371` 结算失败子任务时，将 AgentInvocation 记为 `agent_invocation_failed`，向主 Agent 交付错误说明；没有交付子 Agent 已获得的搜索结果作为部分完成结果。主 Agent 随后只能根据失败通知回答。

Run 的 `completed` 表示主 Agent 已结束这轮回复。它同时包含 `failed` 的 AgentInvocation，不能据此判断用户搜索诉求已经完成。

最终答复猜测“目标智能体不可用或离线”没有这次运行证据支持：目标已完成多轮模型调用与工具执行。

## 本次暴露的改进点

1. **流中断恢复**：区分文字、工具增量、已执行动作和已交付内容；在可证明没有新副作用时，支持从已持久化检查点恢复，避免把整项委托直接丢弃。不能简单放开所有收到 payload 的请求重试。
2. **部分成果回传**：失败交付应带有授权范围内的已完成步骤、证据引用和可恢复状态，让主 Agent 能继续处理并准确说明缺口。
3. **网络异常诊断**：首次搜索的 `ConnectError` 被通用异常路径压成类名且记为不可重试；应保留脱敏后的异常链并分类处理连接类错误。该时间窗的 runtime/sandbox 容器日志没有额外异常栈，无法还原第一次搜索的更底层原因。

本次协作权限与固定绑定路径按预期生效。需要补强的是执行故障后的恢复、成果交付和错误解释。

## 对项目侧因素的补充核查

用户追问后，对实际部署源码、运行账本和容器配置进行了只读复核；没有重放模型请求。

- Run 截止时间为 11:19:43 UTC，故障发生在 10:23:48 UTC，取消标志为空。
- 失败 Runner 最后一次心跳为 10:23:44 UTC，距失败 4 秒；账本没有取消、deadline、租约回收或 Runner 丢失事件。
- 两个 Runtime 容器从 10:06:07 UTC 启动，重启计数为 0，OOM 标志为假。
- 模型流调用没有被项目级 `asyncio.wait_for/timeout` 包裹。ProviderPool 的正常回收路径跳过仍被使用的客户端；Worker 在失败结算后的 finally 中才释放租约。未发现本次请求被这些逻辑提前关闭的证据。
- 当前容器没有 HTTP/HTTPS/ALL_PROXY/NO_PROXY 环境变量，等价 SDK 客户端没有代理映射。这只排查了显式容器代理，不能排除宿主机出口、透明代理或中间网关问题，也不是故障时刻的网络抓包。
- 等价 SDK 客户端配置为连接超时 5 秒、读取超时 600 秒。本次已有有效响应并在约 84 秒后以 chunked EOF 结束，现存记录不支持“命中配置的读取超时”解释。
- 发现项目额外的可观测性不一致：`OpenAILLM` 没有覆盖 SDK 的默认 `max_retries=2`，与代码声称由 ModelAdapter 独占传输重试不一致。因此一条 ProviderAttempt 不保证对应一次物理请求。此问题尚不能认定为本次已开始返回内容的流中断原因。
- 本次失败的 `provider_request_id` 为空。流实现直到正常结束才返回请求 ID；断流时缺少该标识，妨碍向供应商关联排查。

可以确认本次授权与派发成功，并排除账本已覆盖的取消、deadline、租约回收和容器重启路径；不能据此排除所有客户端、SDK、容器网络或上游因素。要把断流归责到具体组件，还需要故障时的连接诊断、网关日志或供应商侧关联证据。

## 模型未完整返回时的实际执行边界

进一步核对了模型流、协调器、工具分派及前端临时草稿路径。此前把“收到 payload 后可能已经执行工具”作为本项目不恢复的理由不准确。

- `agent_loop_coordinator.py:328` 先等待 `ModelAdapter.invoke()` 返回；异常分支直接退出。工具执行入口 `dispatch_batch` 位于其后的第 483 行，流观察回调不会分派工具。
- 正式 Message 和子 Agent 的成功结果在 Worker 结算完成后交付；片段不会提前提交成正式回答。根 Chat/Plan 工作可以通过 SSE 显示 `provisional=true` 的临时文字，失败时发 `model.stream_reset` 清理；本次子任务没有启用该展示。
- 因此，**本次明确抛出断流异常的模型轮次没有新增工具执行或正式结果交付**。之前搜索与抓取是更早已完成轮次的结果，恢复时应保留，不能重新执行。
- 在这个执行模型下，“已收到 payload”本身不足以阻止有限重试；本次无需假设工具片段已经带来副作用。恢复当前未执行的模型轮次，与重放之前已经执行的整个任务是不同操作。

同时发现独立的完整性校验缺口：OpenAI 适配器在流自然结束后会产出 done，即使没有 finish_reason；协调器容许 UNKNOWN，工具调用也可能从缺失终止标记的结果继续执行。不能将“流正常结束”一概等同于“协议完整输出”。

在已部署镜像中用假 SDK 流与空操作执行器做了四项内存验证，没有 DB、外部请求或文件写入：文字/合法工具 JSON 后抛 RemoteProtocolError 均不调用执行器；缺少 finish_reason 的正常 EOF，文字可完成、合法工具 JSON 可进入执行器。后两个场景是另外的完整性问题，本案记录的是前一种显式断流异常。

## 后续修复（2026-09-23）

已按用户指定范围修复部分检索成果回传、断流请求标识保存及 SDK 隐式重试，见[修复与发布记录](UAT_DELEGATION_FAILURE_FIXES_2026-09-23.md)。旧会话记录保持原样，缺失请求 ID 无法追溯补齐。收到 payload 后的恢复策略及协议完整性判定按用户要求暂不修改；本次修复不改变上述故障归因结论。
