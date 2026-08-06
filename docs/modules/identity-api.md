# 身份与 Agent API

本文是平台账号、外部身份、统一用户主体、平台角色、Agent 访问范围和外部执行协议的唯一产品权威来源。物理字段与外键以[数据库契约](../technical/database-schema.md)为准。

## 第一期身份决策

第一阶段 Agent API 采用最小方案：

```text
Agent API Key + caller supplied raw userId
```

API Key 认证调用系统并绑定入口 Agent；调用系统在每次请求中直接传入 `userId`。MyClaw 不验证该 `userId` 对应的终端用户 Token，也不要求 OIDC on-behalf-of 流程。

这个选择用于受控 UAT 和企业内网集成，优先验证 Agent、Run 和协作闭环。它具有必须公开接受的安全事实：

> 持有有效 Agent API Key 的调用方，可以在该部署内断言任意 `userId`，因此也可以冒充该用户访问绑定 Agent 能触达的 MyClaw 用户数据。

第一阶段不通过命名、日志或文档把这个参数包装成“已验证主体”。该模式不得直接开放给浏览器、不受控第三方或公网开发者，也不能满足企业生产身份 Gate。后续升级路径见“身份演进”。

## 统一用户身份

MyClaw 对外使用 canonical `userId`，对内使用不可变 `account_id`：

```text
PlatformAccount
├── account_id                 # 内部稳定主键
├── login/profile
├── platform roles
└── canonical_user_id          # 租户内外部业务标识
```

`PlatformAccount` 是平台登录、授权与数据归属主体。Web、Agent API 和 Channel 必须先把外部身份解析为同一个 `account_id`；Session、Memory、WorkingPlan、Group、Execution、Workspace、Artifact 和所有人类动作只引用 `account_id`。AgentInvocation 传播已经固定的 `account_id`，不为每个 Agent 生成另一套用户身份。

`canonical_user_id` 的唯一范围从第一天起就是 `(tenant_id, canonical_user_id)`。第一阶段虽然只有一个固定 tenant，也不允许建立全局裸 userId 查询路径。`asserted_user_id` 只表示 raw Profile 下调用方提交的原始断言，不能直接作为领域外键。

## 第一阶段平台角色

```text
user
builder
admin
```

### `user`

普通用户可以使用有权访问的已发布 Agent，管理自己 `account_id` 下的 Session、WorkingPlan、Group、Artifact、Verification、Execution 和记忆，并响应 Clarification、Plan 实施、成员邀请和 Tool Confirmation。

### `builder`

Builder 包含普通用户能力，并可以维护自己负责的 Agent、Skill、草稿和版本，查看聚合使用状态和脱敏运行统计。

Builder 拥有 Agent 蓝图，不因此拥有使用该 Agent 的用户数据，也不能获得用户业务权限。

### `admin`

管理员可以管理账号和角色、Model、Platform Tool、Skill、Sandbox Profile 和 Agent，并处理平台故障与治理配置。

管理员默认只查看运行状态、错误、用量和受控脱敏 Trace。读取用户正文属于未来单独的 break-glass 能力，第一阶段不因 `admin` 自动开放。

## Agent 所有权与访问范围

第一阶段 Agent visibility：

```text
private | enterprise
```

- `private`：仅 Owner 和管理员可测试或使用；
- `enterprise`：部署实例内用户可使用。

发现不等于授权。调用目标 Agent 前必须同时满足目标可发现、已发布且启用、Collaboration Policy 允许、原始 `account_id` 有权使用，以及当前 Group/Session/预算允许。

调用链始终继承 Run 创建时固定的 `tenant_id + account_id`，不能变成 Agent Owner、Builder 或管理员身份。目标 Agent 的 Tool Credential 也不能把用户提升为平台角色。

## 用户数据归属

Session、Message、Plan、Group、Execution、Workspace、Artifact 和用户记忆以 `tenant_id + account_id` 作为数据归属：

- 用户只能读取和操作自己 `account_id` 下的数据；
- Builder 不能因为拥有 Agent 而读取使用者数据；
- Agent 聚合统计不得泄露其他用户内容；
- Agent-to-Agent 调用不产生新的数据所有者；
- 管理面不能通过普通接口绕过内容边界。

在第一阶段 API 模式下，平台先用固定 tenant 和 raw userId 解析 `account_id`，这些隔离规则才开始生效。它们可以阻止 Agent 横向越权，但不能阻止 API Key 持有者选择另一个 raw userId 并解析成另一个账号。

## Web 与 Channel 身份

Web 使用平台登录或企业联登把受验证身份映射到 `account_id`。Channel 必须通过 ChannelIdentityBinding 将 Provider 身份映射到已有或新建账号；不能直接复用 Agent API 的裸 userId Header 作为 Channel 身份证明。

Web/Channel 的认证强度与 Agent API 第一期简化方案相互独立。

## Agent API Key

```text
AgentApiKey
├── id
├── agent_id
├── name / caller_label
├── secret_hash / prefix
├── status
├── created_by
├── expires_at
└── last_used_at
```

同一 Agent 可以创建多把 Key并独立命名、审计、撤销和轮换；明文 Secret 只在创建时展示一次。Key 不得出现在 URL、日志、Event 或 Trace 中。

第一阶段所有 Key 使用相同最小能力集。下列 `userId` 只是 API 输入语义；经过解析后，业务查询、授权和外键一律使用 `tenant_id + account_id`：

- 按调用方传入的 `userId` 幂等预创建或取得普通用户账号；
- 创建或继续绑定 Agent 的 Session；
- 发送 Chat、Plan 或 Group 请求；
- 处理该 `userId` 下的 Clarification、Approval、Membership 决定和取消；
- 查询该 `userId` 下的 Run、Event、WorkingPlan、Invocation、Verification、Group 和 Artifact。

Key 不能修改 Agent、Tool、Skill、Model、Policy、Sandbox 或平台角色。Key 被撤销后禁止新请求；已经持久化的 Run 默认继续，需要停止时显式取消。

## raw userId 请求契约

Agent API 请求使用：

```http
Authorization: Bearer <agent-api-key>
X-MyClaw-User-Id: <canonical-user-id>
```

规则：

- Header 必填，规范化后不能为空且必须满足长度/字符集约束；
- 服务先用 `AgentApiKey + asserted_user_id` 验证入口并解析 `tenant_id + account_id`，然后建立请求上下文；不接受 body 中另一份冲突 userId；
- 传入不存在的 userId 时，仅在明确允许的预创建/Session 创建路径幂等创建普通 `user` 账号；
- 外部不能传入 builder/admin 角色、Owner、tenant 或数据 Scope；
- 所有状态命令的幂等作用域为 `tenant_id + account_id + api_key_id + endpoint + idempotency_key`，并保存 `request_hash` 用于检测同 Key 异参数重放；
- API Key、Header 和账号状态在每次新请求校验；持久 Run 的后续 Agent/Tool dispatch 继续携带已固定的 `tenant_id + account_id`，不重新信任外部 Header；
- 日志只记录脱敏/哈希后的 userId（需要业务审计显示原值时使用受控审计字段），绝不记录 Key 明文。

请求上下文至少保存：

```text
api_key_id
asserted_user_id
entry_agent_id
session_id / run_id
request_id
credential_revision
account_authorization_revision
created_at
```

`asserted_user_id` 这一命名用于提醒它来自调用方断言。API 边界使用 `(fixed_tenant_id, asserted_user_id)` 解析或在允许的路径创建 `PlatformAccount`，得到 `account_id`；进入领域对象后只使用 `account_id`。第一阶段没有密码学证据证明终端用户本人参与了该请求。

## userId 更新语义

canonical `userId` 不作为普通资料字段任意修改。企业身份发生变化时，第一阶段默认解析为新账号：

- 不自动迁移、合并或重新归属旧 Session、Memory、Group、Execution 或 Artifact；
- 目标 userId 已存在时使用既有账号，不能复制同一身份；
- 账号合并和历史迁移后置为独立、高风险、可审计能力。

## 持久异步 API

Agent API 以持久化异步 Run 为权威协议：

```text
API Key + raw userId
→ 发送消息并创建 Run
→ 返回 run_id 与当前状态
→ 查询或订阅 ExecutionEvent
→ 提交批准、Clarification 或 Tool Confirmation
→ 取得最终结果与 Artifact
```

HTTP 断开不会取消 Run。所有状态命令必须携带 `Idempotency-Key`；相同 Key 和相同语义返回原对象，不同语义返回冲突。

具体资源、Header、响应、错误和 SSE 契约见[当前 API 与 SSE](../technical/api-sse.md)。

## 生产身份 Profile

部署必须声明且只启用一个身份 Profile；同一路由不得在运行时猜测认证语义：

| 部署 Profile | 允许环境 | 允许的认证来源 | 是否允许 `X-MyClaw-User-Id` |
| --- | --- | --- | --- |
| `raw_user_id_v1` | dev / 受控 UAT | `web_session`（已登录的本地/UAT 账号）、Agent API Key（caller 自行断言用户） | 仅 Agent API Key 请求必填 |
| `enterprise_oidc_v1` | 企业生产 | `oidc_user`、`oidc_obo`、`service_principal` | 禁止 |

`enterprise_oidc_v1` 必须同时实现用户 access token 和 OBO；纯后台自动化使用同一 Profile 下的 service principal。浏览器可在 OIDC callback 后使用 HttpOnly 会话 Cookie，但 AuthContext 的身份证据来源仍标记为 `oidc_user`，不降级成 UAT `web_session`。`raw_user_id_v1` 不得通过风险签字进入生产。

AuthContext 的 revision 来源固定：`credential_revision` 在 UAT local web 取本地会话 revision、Agent API 取 AgentApiKey.credential_revision、生产 Cookie 取 BrowserAuthSession.credential_revision、直接 oidc_user Bearer 取 TenantIdentityProvider.config_revision、oidc_obo/service_principal 取 WorkloadClientPolicy.credential_revision；`account_authorization_revision` 始终取 PlatformAccount.authorization_revision。生产请求还必须实时确认 ExternalIdentity/IdP/Workload policy active，不能用旧 Snapshot 绕过撤销。

生产浏览器固定使用 Authorization Code + PKCE(S256)。登录事务的 `state`、`nonce` 和 PKCE verifier 都是一次性、最长 10 分钟；redirect URI 必须与租户预配置值精确匹配，`return_to` 只接受站内 allowlist 路径。callback 在服务端换取并验证 Token，浏览器不把 access/refresh token 写入 LocalStorage、SessionStorage 或可读 Cookie。登录成功后轮换为至少 128 bit 熵的 opaque session token，数据库只保存 token hash；Cookie 必须 `Secure; HttpOnly; SameSite=Lax; Path=/`，空闲 30 分钟、绝对 8 小时到期，登录、权限变化、账号/ExternalIdentity 撤销和显式登出都撤销/轮换 Session。

所有使用 Cookie 认证的 POST/PUT/PATCH/DELETE 同时要求同源 `Origin`（无 Origin 时校验 Referer）和与 Session 绑定的 `X-CSRF-Token`；Bearer/OBO/service-principal 请求不得因缺少 CSRF Header 被拒绝。带 credential 的 CORS 只允许精确来源，禁止 `*`。CSRF 校验发生在读取业务 body 和创建 IdempotencyRecord 之前；失败返回 403 `forbidden` 并写安全审计。

### OIDC/OBO 验证契约

每个生产请求必须完成：

1. 只信任租户预配置的 HTTPS issuer，按缓存和轮换策略取得 JWKS；
2. 验证签名、`iss`、`sub`、`aud`、`exp`、`nbf`，拒绝缺失或算法不允许的 Token；
3. OBO 同时验证受信 workload client、delegated scope 与目标用户 subject，禁止客户端直接提交替代 subject；
4. 以 `(tenant_id, issuer, subject)` 查询 `external_identity` 并得到稳定 `account_id`；生产普通业务路径不隐式创建高权限账号；
5. 生成不可变 AuthContext：`tenant_id, account_id, external_identity_id, authentication_source, client_id, scopes, credential_revision, account_authorization_revision, request_id`；认证来源/客户端变化递增前者，角色/账号授权输入变化递增后者；
6. 领域授权仅使用 AuthContext，不从 body、query、Agent 输出或 Tool 参数重新读取主体；
7. Token 撤销/账号停用阻止新请求；已持久 Run 按固定 account 执行，安全 Owner 可用取消/冻结命令停止后续副作用。

### 生产账号预配

`enterprise_oidc_v1` 默认 `account_provisioning=preprovisioned`。管理员/企业目录先创建普通 PlatformAccount，再把受信 `(issuer,subject)` 绑定为 ExternalIdentity；未知 subject 即使 Token 有效也返回 `identity_mapping_required`，普通业务请求不得静默 JIT 建号。绑定、换绑和撤销需要 account version CAS、Idempotency-Key、`identity.manage` 管理 Scope 和 PlatformAuditEvent；同一 tenant/issuer/subject 同一时刻最多一个 active account 绑定。换绑必须先撤销旧行、保留历史，再创建新行，不允许原地改写 account_id。email、UPN 和 display name 只作资料，不能作为登录关联键。

### service principal 约束

Service principal 必须映射到独立 `account_type=service` 的 PlatformAccount，不持有 `user|builder|admin` 人类角色，只通过受信 WorkloadClientPolicy 的显式 Scope 授权。它可以拥有自己的 Session 和 Artifact，不能回答或批准目标为自然人账号的 Clarification/Approval，也不能通过传入 userId 借用自然人数据。

## 演进顺序

身份实现按以下顺序交付，但生产准入直接以以上 Profile 契约为准：

### Stage 1：受控委托身份（可选过渡，不是默认生产参考）

把 API Key 升级为 `TrustedIdentityClient`，限制可断言的 tenant、issuer、用户 namespace 和 Agent，并使用 mTLS、签名 subject assertion 或短期 client credential。仍由企业后端代表用户，但不再允许所有 Key 断言所有 userId。

### Stage 2：终端主体 Token（生产参考）

支持 OIDC access token、token exchange 或 on-behalf-of。MyClaw 验证 `(tenant_id, issuer, subject)`，映射内部 `account_id`；API Key/mTLS 只能补充认证 workload client，不能替代用户 Token。

### Stage 3：多租户与联邦

在 Stage 2 已有 ExternalIdentity、OBO 和 service principal 之上，增加同一控制域的多客户 Tenant 运营身份、租户级 Registry/配额边界、受审计身份链接/恢复、跨控制域 delegation chain 和远程 Agent Gateway 授权。`enterprise_production_v1` 在完成该阶段及其隔离/计费/合规证据前仍是一控制域一企业 Tenant。

升级时必须兼容既有 canonical userId 或提供显式迁移工具，不能静默改变历史数据 Owner。

## 当前禁止事项

- 把 raw userId 模式称为已验证终端身份或企业生产安全；
- 把 Agent API Key 暴露给浏览器、移动端或第三方脚本；
- 允许调用方通过 userId Header 指定角色、Owner、tenant 或 Tool 权限；
- 在 body/Header 中出现两份冲突 userId 后静默选择其一；
- 在日志或 Trace 中保存 API Key；
- 让 AgentInvocation 改写原始 userId；
- 在没有迁移审计时合并两个 userId 的历史数据。
- 在生产普通登录/业务请求中为未知 OIDC subject 自动 JIT 建号，或按 email/display name 猜测账号绑定。
