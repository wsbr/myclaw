# 模型参数与 Runtime 请求边界

模型默认参数只配置推理选项。System 指令、消息、工具列表、目标模型与工具选择由 Runtime 装配，不能从模型配置中覆盖。

## 已支持参数

聊天模型的 `default_parameters` 白名单为：

- `temperature`
- `max_tokens`
- `max_output_tokens`
- `extra_body`

Embedding 模型仍使用 `dimensions` 和 `encoding_format`，并保留原有维度与编码校验。

`extra_body` 目前只接受 `enable_thinking`，值必须是原生 JSON boolean。该扩展用于现有 OpenAI-compatible 推理模式；Conformance 与维护任务需要强制工具选择时仍可将其设为 `false`。Anthropic adapter 不支持该扩展，会明确拒绝非空 `extra_body`。以后支持其他 Provider 扩展时，应先增加有类型约束的白名单与实际 SDK 请求回归，不开放任意请求体透传。

`model_id`、`tool_choice`、`response_format` 仅允许可信 Runtime 在校验默认参数之后设置。当前 Anthropic adapter 不支持 `response_format`，收到时明确报错，不静默忽略。`system`、`messages`、`input`、`instructions`、`tools`、`model`、`stream` 等请求字段不允许写入默认参数，也不允许经 `extra_body` 注入。未知参数同样拒绝，包括 Provider 原生搜索开关 `enable_search`；能力授权仍来自平台工具绑定。

## 注册与历史版本

`ModelCreate`、`ModelVersionCreate`、`ModelPatch` 在参数进入存储前校验；错误返回参数校验失败。原有“不允许参数内嵌凭证”与输出额度校验继续生效。

历史不可变 ModelVersion 不原地修改。Worker、维护任务与 Conformance 在展开默认参数前再次检查；不符合当前白名单的版本以 `model_parameters_invalid` 失败，需发布符合要求的新模型版本。错误不可重试，诊断只说明问题字段或允许的扩展，不回显配置正文。

OpenAI 和 Anthropic 的 `chat`、`stream`、`count_tokens` 出口都校验参数，避免绕过注册校验或通过 SDK 的 extra-body 合并覆盖已装配消息和工具。Token 计数不能把这类错误降级为估算值；普通计数服务不可用仍保留原有估算降级。

## 验证

`test/unit/backend/test_model_parameter_boundary.py` 覆盖注册参数、旧冻结版本、维护/Conformance、不可重试诊断、计数降级边界，以及实际 OpenAI/Anthropic SDK 通过 `httpx.MockTransport` 生成的请求体。测试不请求外部模型。

Anthropic 0.40 SDK 的计数接口位于 `beta.messages.count_tokens`；适配器优先使用稳定接口，在旧 SDK 上使用同等 beta 接口。两条路径均在参数校验之后执行。
