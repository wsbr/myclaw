# Channel 与通知

本文是第三方消息通道、Session 绑定和执行通知的唯一权威来源。

## 定位

Channel 是平台级双向消息接入和通知投递能力。它将飞书、QQ、微信等第三方账号或机器人连接到 MyClaw Session。

Channel 不是 Platform Tool，不改变 Agent 装配，也不是 Agent 间的新通信方式。Agent 间工作通过 AgentInvocation 或 Group Message 进行。

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
- `ChannelIdentityBinding`：将经过 Channel Provider 验证的第三方 subject 关联到平台内部 `account_id`；
- `ChannelConversationBinding`：将外部 conversation 绑定到一个 Session；
- `NotificationDelivery`：记录一次系统通知在原 Channel 的投递与重试。

Channel 是可选部署能力；未启用时不影响核心生产 Profile。任何进入 `enterprise_production_v1` 的 Channel Plugin 必须随平台镜像或签名制品发布，固定 implementation digest、配置 Schema、Provider 验签方案、出入站限额和 Conformance 结果，且通过供应链/Secret/重放测试；不允许从管理页面上传未签名代码。插件 Marketplace 与第三方分发仍是后续产品能力，但不会形成第二套运行时或身份契约。

## 入站消息

插件标准化后的消息至少包含：

```text
channel_key
channel_account_id
external_user_subject
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
4. 验证 Provider 签名/凭证，并按 `channel_account_id + external_user_subject` 查询 ChannelIdentityBinding，得到内部 `account_id`；
5. 查找 ChannelConversationBinding；
6. 不存在时创建 Session 并绑定，存在时持续复用原 Session；
7. 将附件写入 Session Sandbox 并按需要登记 Artifact；
8. 写入 Message 并创建对应 Run。

外部 conversation 一旦绑定 Session，就持续复用。闲置不会自动创建新 Session；用户通过明确的“新会话”操作解除并创建新 Session。

Channel 不能产生第二套用户身份，也不能让插件用裸 `userId` 冒充用户。插件必须提供可验证的 Provider 身份事实并完成 ChannelIdentityBinding；无法确认 ChannelAccount、external subject 与内部 `account_id` 的绑定时不能进入正式 Agent Session。

## Session 来源

Session 保存稳定来源：

```text
origin: web | api | channel
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
- 等待 Plan 实施、成员邀请或其他 Approval；
- 等待风险 Tool Confirmation；
- 等待受限 Session Grant 确认或重新授权；
- Run 完成、部分成功、失败或取消。

Group 中任意 Agent 发起的 Clarification 仍投递到原 Session Channel，并在消息中标明发起 Agent。回复携带 `session_id + clarification_id`，只唤醒对应 Agent 的 Work Item。

投递失败只在可证明安全时使用同一 provider idempotency key 重试原 Channel，不扩散到其他渠道，也不改变 Run 的业务状态。请求可能已送达但 Provider 无幂等/查询能力时进入 `outcome_unknown`，禁止盲目重发。原 Channel 被解绑或不可用时，平台保留站内待处理状态和失败记录。

后期账号设置可以增加简单覆盖：

```text
notification_mode:
- session_origin
- default_channel
- all_channels
```

默认始终是 `session_origin`。

`enterprise_production_v1` 明确不支持 Group 外 Agent 主动定向联系其他岗位人员；当前 Agent 只能通过原 Session 来源、已存在 Group Membership 或显式 Approval/Clarification 目标与人交互。未来若开启该能力，必须在新 Profile/ADR 中先完整定义 account 目标解析、个人通知偏好、组织路由、授权、API 和审计；不能把该能力视为当前 NotificationDelivery 的隐式延伸。

## 渠道能力差异

ChannelPlugin 负责将平台交互降级到渠道支持的形式：

- 支持卡片或按钮时可直接确认；
- 不支持时使用文本指令或安全链接；
- 大文件通过受控 Artifact 下载链接交付；
- 渠道消息大小限制由插件切分或摘要。

降级只改变表现形式，不能改变 Plan Approval、成员邀请、Clarification、Tool Confirmation、Session Grant 和取消的领域状态。
