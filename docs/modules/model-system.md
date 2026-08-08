# Model

本文是模型注册、Agent 选择、Provider 兼容和失败语义的唯一权威来源。

## Platform Model Registry

模型使用逻辑项与不可变版本分离：

```text
Model
├── id / key / name / status
└── current_version_id

ModelVersion (immutable)
├── provider / model_name / endpoint
├── encrypted credential / default_parameters
├── capabilities / adapter protocol
└── content_hash / published_at

ConformanceRun (append-only evidence)
├── model_version_id / suite_version
├── status / result_summary / evidence_artifact
└── started_at / ended_at
```

管理页面直接接收完整 Provider API Key，HTTP 请求字段是 write-only；后端使用部署级 `ENCRYPTION_KEY` 做带认证加密，数据库只保存带版本前缀的密文。查询接口仅返回是否配置及存储来源，明文和密文都不能进入 Agent 配置、Prompt、事件或 Trace。历史记录和旧版内部路径保留 `env://` 解析兼容，但 v2 ModelVersion 公开写接口不再接收引用。Draft 只选择逻辑 `model_id` 并显示当前解析预览；发布 AgentVersion 或 draft_test_snapshot 时固定精确 `model_version_id + content_hash`。Endpoint、model name、API Key、参数、能力或 Adapter 协议变化必须发布新 ModelVersion。

Conformance 结果不写回 ModelVersion。每次测试创建新的 ConformanceRun，API 中的 `latest_conformance` 只是查询投影；准入以当前必需 suite 的最新 terminal passed 证据为准，不改写历史版本。

## 能力声明

至少声明：

```text
streaming
native_tool_calling
parallel_tool_calling: boolean
tool_result_continuation
usage_in_stream
stop_reason
context_window
structured_output: json_schema | json_object | none
```

结构化输出是可选能力，不再作为三模式普通最终响应的必要条件。三模式最低协议是普通 Assistant Message、native Tool Calling、Tool Result 续轮和 stop reason。

能力声明必须由 Conformance Suite 真实探测，不能根据 Provider 名称或文档静态猜测。

## Agent 选择

每个不可变 AgentVersion 固定 `model_id + model_version_id`：

- Chat、Plan、Group 成员都使用自身 AgentVersion 的模型；
- Session 和单次用户请求不覆盖模型；
- AgentInvocation 目标使用目标 AgentVersion 的模型；
- 运行中不因为错误静默切换默认模型；
- Model 新版本只影响后续 Draft 解析；已发布 AgentVersion 采用新版必须重新测试并发布新的 AgentVersion；
- ModelVersion 停用阻止新的发布和 Run 准入，但不改写已有执行事实；运行中不得静默切换。

未来若支持模型路由，必须是可观察、版本化、保留 Agent 语义的独立能力，不能作为隐式 fallback。

## Provider Conformance

连通测试必须覆盖：

- 中文流式普通消息；
- 单 Tool Call；
- 多轮 Tool Result；
- 相同 Tool 类型连续两次业务调用；
- 并行 Tool Call（若声明支持）；
- Tool 参数 schema 错误与一次修复；
- 普通最终消息和 stop reason；
- usage；
- 零 payload 传输错误安全重试；
- payload 后错误不盲目重放；
- 长 Context 和 Compaction 边界；
- `update_plan`、`ask_user`、`invoke_agent` 等 Runtime Coordination Tool。

只完成普通文本问答，或只完成一次结构化 JSON，不足以标记为适合三模式。

## Adapter 边界

Adapter 只转换：

- system/user/assistant/tool message；
- 有序 Turn Item 与 text、file/artifact reference、structured data 等 content part；
- Tool Definition、Tool Call 和 Tool Result 关联；
- stream delta 与完整消息；
- stop reason、usage 和错误；
- Provider 原生 reasoning summary 的受控投影和不透明 continuation state 引用。

Adapter 不做：

- 权限计算；
- Tool 类型消费；
- Plan/Group 业务编排；
- 最终结果评价；
- 文本 Tool Call 解析；
- Provider 不兼容时静默换模型。

Adapter 必须保持 Item 顺序、Tool 关联和供应商不透明 continuation state，不能把 provider state 解释为业务完成、权限或可共享推理内容。

普通最终消息不得强制套入 `finalize_response`、Completion Envelope 或统一 Outcome JSON。真正需要机器输入时，由单个 Tool 的小型 schema 约束。

## 重试和失败

Runner 记录实际 Provider、模型标识、参数版本、AgentTurn 和 ProviderAttempt。

透明 Provider 重试仅在同时满足时允许：

- 尚未收到任何 payload；
- 尚未产生 Tool Call；
- 尚未提交任何业务副作用；
- 错误被标记为瞬时可重试；
- 仍在 Attempt 和预算上限内。

收到部分文本、Tool Call 分片或副作用后不得整轮重放。失败必须区分 authentication、rate limit、timeout、transport、context length、protocol、content filtering 和 provider internal error。

## 管理界面

模型页展示 Endpoint、Credential 状态、参数、能力、探针版本、最后结果和启停。Agent 页面只显示可选择模型及对 Builder 有用的延迟、成本和能力摘要。

Provider ModelVersion 未通过当前 Conformance Suite 时不能被新 AgentVersion 发布为三模式模型。管理页对逻辑 Model 和不可变 ModelVersion 分开展示；编辑配置实际创建新版本，不原地修改历史版本。
