# Channel 与通知

本文是第三方消息通道、Session 绑定和执行通知的唯一权威来源。

## 定位

Channel 是平台级双向消息接入和通知投递能力。它将飞书、QQ、微信等第三方账号或机器人连接到 MyClaw Session。

Channel 不是 Platform Tool，不改变 Agent 装配，也不是 Agent 间的新通信方式。Agent 间执行请求仍只能使用 `invoke_agent`。

## 核心实体

```text
ChannelPlugin
ChannelAccount
AgentChannelBinding
ChannelIdentityBinding
ChannelConversationBinding
NotificationDelivery
```

- `ChannelPlugin`：适配第三方协议、验签、消息标准化和回复方式；
- `ChannelAccount`：平台账号开通并授权的具体渠道账号或机器人；
- `AgentChannelBinding`：将渠道机器人或入口绑定到一个入口 Agent；
- `ChannelIdentityBinding`：将第三方用户身份关联到平台唯一 `userId`；
- `ChannelConversationBinding`：将外部 conversation 绑定到一个 Session；
- `NotificationDelivery`：记录一次系统通知在原 Channel 的投递与重试。

Channel Plugin 的安装、审核和分发机制后续确定，不影响上述运行时契约。

## 入站消息

插件标准化后的消息至少包含：

```text
channel_key
channel_account_id
external_user_id
external_conversation_id
external_message_id
content
attachments[]
reply_to?
```

平台处理流程：

1. 验证插件和 ChannelAccount；
2. 使用 `external_message_id` 幂等去重；
3. 根据 AgentChannelBinding 找到入口 Agent；
4. 将外部身份解析为唯一 `userId`；
5. 查找 ChannelConversationBinding；
6. 不存在时创建 Session 并绑定，存在时持续复用原 Session；
7. 将附件写入 Session Sandbox 并按需要登记 Artifact；
8. 写入 Message 并创建对应 Run。

外部 conversation 一旦绑定 Session，就持续复用。闲置不会自动创建新 Session；用户通过明确的“新会话”操作解除并创建新 Session。

Channel 不能产生第二套用户身份。插件必须提供可信 `userId`，或先完成 ChannelIdentityBinding；无法确认身份时不能进入正式 Agent Session。

## Session 来源

Session 保存稳定来源：

```text
origin_type: web | api | channel
origin_channel_account_id?
origin_external_conversation_id?
```

来源在 Session 生命周期内不随用户最近使用渠道自动变化。Chat 中创建的 Plan 或 Group 继承原 Session 来源。

## 通知跟随 Session

第一阶段核心闭环不以完整 Channel 插件生态为验收前提；如果部署启用了 Channel，则不维护用户在线状态、最近活跃渠道或通知路由策略，任务相关通知始终回到 Session 原来源：

- Channel Session：投递到原 ChannelAccount 和外部 conversation；
- Platform Web Session：通过站内状态和 ExecutionEvent 展示；
- Agent API Session：由调用方轮询或 SSE 获取，不主动改投 Channel。

Channel Session 中以下事件可以创建 NotificationDelivery：

- 等待用户 Clarification；
- 等待 Plan / Group 图确认；
- 等待风险 Tool Confirmation；
- Run 完成、部分成功、失败或取消。

Group 中任意 Agent 发起的 Clarification 仍投递到原 Session Channel，并在消息中标明发起 Agent。回复携带 `session_id + clarification_id`，只唤醒对应 Agent 的 Work Item。

投递失败只重试原 Channel，不扩散到其他渠道，也不改变 Run 的业务状态。原 Channel 被解绑或不可用时，平台保留站内待处理状态和失败记录。

后期账号设置可以增加简单覆盖：

```text
notification_mode:
- session_origin
- default_channel
- all_channels
```

默认始终是 `session_origin`。

后期垂类 Agent 定向联系其他岗位人员时，投递目标属于该 `userId` 的个人入口，不是 Session 来源通知，也不会把目标人加入原 Group。该能力可以复用可恢复 `waiting_user` 和 NotificationDelivery，但其目标解析、个人通知偏好与组织路由需要另行设计。

## 渠道能力差异

ChannelPlugin 负责将平台交互降级到渠道支持的形式：

- 支持卡片或按钮时可直接确认；
- 不支持时使用文本指令或安全链接；
- 大文件通过受控 Artifact 下载链接交付；
- 渠道消息大小限制由插件切分或摘要。

降级只改变表现形式，不能改变 Plan / Group 确认、Clarification、Tool Confirmation 和取消的领域状态。
