# 事件、Trace 与恢复摘要

## 关键对象

- 自然 Chat Session：`019fc65f-7cd9-7831-ad06-ce5ef77ddbfa`
- 自然 Plan Run：`019fc662-1ef7-7a9a-a0ee-30546c9e192b`
- 自然 Group Session：`019fc667-ea9b-7765-8637-019c82a46724`
- 首个 GroupRun：`019fc669-c404-79e2-96c3-5b308af42837`
- 技术控制 Chat Session：`019fc67b-2b7e-7b24-a3e3-f3df8cd7941d`
- 中文 Artifact：`019fc683-d9fe-7af7-9f36-407aeada606d`

## 版本冻结

- 总控 v1：`019fc65d-0cc7-7c00-ae7c-ad8637dd6bb6`
- 总控 v2：`019fc678-60f4-72bd-a6d8-adef66d618dc`
- 新 Chat Run `019fc679-6fcd-795a-9048-d6cd0eaaed94` 使用 v2 并返回 `UAT-V2`。
- 已冻结 Plan 和全部既有 GroupRun 仍引用总控 v1，未发生版本漂移。

## Tool 治理

- `execute` 待确认调用：`019fc67d-aa35-79c1-b53e-64a198ac513f`。
- Tool 停用后批准：调用以 `tool_disabled` 失败，`attempt_count=0`，没有创建 Tool Attempt，证明未 dispatch。
- Tool 重新启用并设为 `never`：新调用 `019fc681-9ece-72e1-96bf-4085e53c21ad` 无确认成功，真实输出 1217。
- 验收结束时 `execute` 已恢复 `enabled + always`，配置版本为 5。

## Runner 恢复与 fencing

- Proposal Work Item：`019fc687-135e-7c87-a2ea-093df62e80be`。
- epoch 1 Runner：`019fc687-1361-7abe-af1a-f97c65b28531`，对应 Runtime Worker 重启后状态为 `lost`。
- epoch 2 Runner：`019fc689-4815-7040-b935-1cc10cfb301d`，由持久队列重新接管，随后因结构校验失败进入再次修复。
- 同一 Work Item 的 epoch 3 最终成功；旧 Runner 保持 `lost`，没有产生重复 `plan.draft_ready`。
- 另一个澄清暂停 Work Item `019fc67b-2c37-7112-931f-10267e1e73df` 在用户回答后由 execution epoch 1 升至 2 并成功，澄清记录为 answered。

## 暂停时可观测性快照

快照时间：`2026-08-03 08:23:49`。

- ExecutionEvent：503。
- Runner Trace：completed 67、failed 32、running 3。
- OTel Span：ok 198、error 4，其中 generation span 98。
- AgentInvocation：3 条 succeeded，均来自自然 Plan；自然 Group 为 0。
- Outbox：completed 103、pending 4。
- Platform Audit：42。
- 30 个 Proposal 保持 30 Run / 30 Plan / 30 Work Item；每个 Run 最多一个 `plan.created` 和一个 `plan.draft_ready`。
- Group 技术控制 Run `019fc68f-fbd7-798f-adb4-d948070fd769` 暂停时仍为 `settling`，ToolInvocation 为 0。

## 安全

- UAT 容器日志扫描：模型密钥模式、账号密码和 Authorization Header 命中均为 0。
- ExecutionEvent payload 扫描：模型密钥模式、账号密码和 Authorization 字样命中均为 0。
