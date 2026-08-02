# Model

本文是模型注册、Agent 选择和失败语义的唯一权威来源。

## Platform Model Registry

模型由平台统一注册和治理。记录至少包含：

```text
id
provider
model_name
endpoint
credential_ref
default_parameters
capabilities
enabled
```

`credential_ref` 指向安全存储中的凭证，不能把明文凭证注入 Agent 配置或 Prompt。`default_parameters` 由平台管理员为该模型维护，包括 temperature、top_p、max_tokens 等 Provider 参数。`capabilities` 用于平台兼容性判断和展示。

## Agent 选择

每个 Agent 只选择一个 `model_id`：

- Agent 不保存或覆盖底层采样参数；
- Session 不选择模型；
- 单次请求不携带模型覆盖；
- Group 节点使用各自 Agent 已装配的模型；
- 入口 Agent 使用自己的模型生成初始 Group 图。

这样模型选择是 Agent 蓝图的一部分，而不是运行时偏好。

## 运行与失败

Runner 启动时解析模型并记录实际 Provider、模型版本标识和平台参数版本。若模型不存在、停用、凭证不可用或 Provider 调用失败，平台明确报告失败。

Runtime 不切换到默认模型，也不在请求级 fallback 到其他模型。若未来需要高可用路由，应作为 Model Registry 内部、可观察且不改变 Agent 语义的独立设计重新讨论，不能暗中实现。

## 管理界面

模型管理页负责 Provider 连接、Endpoint、凭证、默认参数、能力、启停和连通测试。Agent 页面只显示可选择模型及对装配者有用的能力摘要。
