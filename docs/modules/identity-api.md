# 身份与 Agent API

本文是平台账号、统一用户身份、平台角色、Agent 访问范围、Agent API Key 和外部执行协议的唯一权威来源。

## 统一身份模型

MyClaw 面向企业私有化部署。一个部署实例接入同一企业用户中心，并以唯一的 `userId` 识别用户：

```text
PlatformAccount
├── 登录与账号资料
├── 基础平台角色
└── canonical userId
```

`PlatformAccount` 是认证与资料外壳，`userId` 是 Session、Memory、Plan、Group、执行记录和 Artifact 的数据身份。平台不为 Web、API、Channel 或不同 Agent 生成多套用户 ID。

`userId` 在部署实例内必须唯一。若未来支持多企业租户，唯一键扩展为 `tenant_id + userId`，但同一租户内仍只有一个身份。

## 第一阶段平台角色

第一阶段只提供三个基础角色：

```text
user
builder
admin
```

### `user`

普通用户可以使用有权访问的已发布 Agent，管理自己的 Session、Plan、Group、Artifact、Execution 和记忆，并响应 Clarification、图确认和 Tool Confirmation。

普通用户不能装配 Agent、上传 Skill、管理平台 Registry 或读取其他用户数据。

### `builder`

Builder 包含普通用户能力，并可以：

- 创建和维护自己负责的 Agent；
- 保存草稿、发布新版本和停用 Agent；
- 使用自己的草稿进行受控测试；
- 上传和维护 Skill；
- 查看自己 Agent 的绑定关系、聚合使用状态和脱敏运行统计。

Builder 拥有 Agent 蓝图，不因此拥有使用该 Agent 的用户数据。它不能读取用户会话全文、私有记忆、Artifact 内容、完整 Prompt 或业务 Tool 原始敏感结果，也不能获得用户的业务权限。

### `admin`

管理员可以管理账号和角色、Model Registry、Platform Tool Registry、Skill、Sandbox Profile 和所有 Agent，并处理平台故障与治理配置。

管理员默认只能查看运行状态、错误、用量和受控脱敏 Trace。读取完整用户内容属于未来单独审计能力，第一阶段不因 `admin` 角色默认开放。

第一阶段不拆分 Auditor、Operator 等角色，也不建设自定义角色与权限表达式。

## Agent 所有权与访问范围

Agent 记录维护所有者和第一阶段访问范围：

```text
visibility: private | enterprise
```

- `private`：仅 Agent 所有者和管理员可用于测试与执行；
- `enterprise`：部署实例内的普通用户均可使用。

第一阶段不提供部门、组织树、用户组、白名单或属性规则。Agent 草稿只能由所有者和管理员测试，不能作为其他 Agent 的正式调用目标。

显式绑定不是授权。在 Chat 中真正调用目标 Agent 前必须同时满足：

```text
调用方已直接绑定目标 Agent
∩ 调用方显式装配 invoke_agent
∩ 目标 Agent 已发布且启用
∩ 当前 userId 有权使用目标 Agent
```

Plan / Group 在用户确认图时执行同样的发布、启停和用户访问校验，并将通过校验的参与 Agent 与 AgentVersion 固定为本次执行授权。执行期间 Agent 被停用或改变访问范围，不把已确认参与者移出当前 PlanExecution 或 GroupRun；它只阻止新 Chat Run、新 Proposal 和后续新执行。模式内 `invoke_agent` 仍需校验目标属于已确认集合。

调用链始终继承原始 `userId`，不能变成 Agent 所有者、Builder 或管理员身份。

## 用户数据归属

Session、Message、Plan、Group、Execution、Artifact 和用户记忆以 `userId` 作为数据归属。默认访问规则是：

- 用户只能读取和操作自己 `userId` 下的数据；
- Builder 不能因为拥有 Agent 而读取使用者数据；
- Agent 聚合统计不得泄露其他用户的消息或业务内容；
- 管理面排障不应通过普通接口绕过内容访问边界；
- Agent-to-Agent 调用不产生新的数据所有者。

## 三种账号进入方式

### 平台登录

用户通过平台账号登录。账号创建时具有平台预设的 `userId`。用户可以在自己的账号管理中完成企业联登认证，并用已验证的企业 `userId` 更新账号。

### 直接企业联登

用户直接从企业 IdP 进入。平台根据经过验证的企业 `userId` 查找账号；不存在时创建普通用户账号，存在时进入原账号。

### Agent API Key 预创建

可信外部系统在调用 Agent 前，使用 `AgentApiKey + userId` 调用账号预创建接口。平台幂等创建或取得该 `userId` 对应的普通用户账号。

用户之后可以用同一个企业 `userId` 直接联登平台，并进入此前通过 API 预创建的账号。API 预创建不产生另一套“外部用户”身份。

通过联登或 API 预创建的新账号只获得 `user` 基础角色，不得由外部传入更高平台角色。

## userId 更新语义

一切数据归属只认 `userId`。账号更新为另一个 `userId` 时，平台将其视为新人：

- 不迁移旧 Session、Memory、Plan、Group、执行记录或 Artifact；
- 不合并新旧 `userId` 的数据；
- 旧数据继续归属于旧 `userId`；
- 新 `userId` 只能访问新身份下的数据。

如果目标企业 `userId` 已经存在，联登必须进入该既有账号，不能复制同一个 `userId`，也不能把临时账号数据合并过去。临时账号可以被标记失效，但数据仍保留原 `userId` 归属。

## Agent API Key

API Key 是绑定某个 Agent 的外部接入凭证，不是平台管理员 Token，也不是终端用户身份。

```text
AgentApiKey
├── id
├── agent_id
├── name / caller_label
├── secret_hash
├── prefix
├── status
├── created_by
├── expires_at
└── last_used_at
```

同一 Agent 可以创建多把 Key。每把 Key 可以独立命名、审计、撤销和轮换。轮换允许新旧 Key 在受控时间内并存；明文 Secret 只在创建时展示一次。

第一阶段不提供自由组合 Scope。所有 Key 使用同一最小能力集：

- 幂等预创建或取得普通用户账号；
- 创建或继续绑定 Agent 的 Session；
- 发送消息并选择 Chat、Plan 或已启用的 Group；
- 确认 Plan / Group 图；
- 回复指定 Clarification；
- 确认或拒绝风险 ToolInvocation；
- 查询 Run 状态、ExecutionEvent 和结果；
- 下载当前 `userId` 有权访问的 Artifact；
- 取消当前 Run。

API Key 不能修改 Agent、Tool、Skill、Model、Policy、Sandbox 或平台配置。它只能把绑定 Agent 作为入口；运行中的 Agent 仍可按自身装配通过 `invoke_agent` 调用其他 Agent。

API Key 被撤销后禁止发起新请求。已经启动的持久化 Run 默认继续执行，需要停止时应显式取消 Run。

## API userId 与审计

可信企业后端完成自己的登录和业务授权后，在每次 Agent API 调用中传入 `userId`。平台信任该断言并将同一 `userId` 传播到完整 Agent 调用链。

每次调用至少审计：

```text
api_key_id
userId
entry_agent_id
session_id
request_id
created_at
```

业务角色和业务数据权限由外部系统、垂类 Agent、Skill 指导和业务 Tool 共同处理。API Key 不替代业务授权，也不能通过 `invoke_agent` 提升原始用户权限。

## 持久异步 API

Agent API 以持久化异步 Run 为权威协议：

```text
发送消息
→ 创建 Run
→ 返回 run_id 与当前状态
→ 查询或订阅 ExecutionEvent
→ 提交图确认、Clarification 或 Tool Confirmation
→ 取得最终结果与 Artifact
```

外部系统可以轮询，也可以通过 SSE 按 sequence 持续读取。HTTP 连接断开不会取消 Run。快速 Chat 可以提供短等待包装，但不能形成第二套同步执行模型。

所有改变状态的请求必须携带 `idempotency_key`。相同 Key 和相同请求语义重复提交时返回原对象，不能重复创建 Session、Run、Message、Confirmation 或取消操作。

API 至少需要表达：

- Session 创建与读取；
- Message / Run 创建；
- Run 状态和 ExecutionEvent 游标查询；
- SSE 事件流；
- Plan / Group Proposal 确认或用户修改；
- Clarification 定向回复；
- Tool Confirmation 确认或拒绝；
- Run 取消；
- Artifact 查询与下载。

Webhook 和外部通知不是第一阶段 API 的前提，相关设计见 [Channel](channel-system.md)。
