# V1 Platform Tool 与 Skill（历史实现参考）

> **历史 V1 实现参考，非目标设计。** Registry、Invocation 和 Skill 包只在当前方案确认的范围内迁移复用；Tool 持续可见、具体 Invocation 幂等、Session Grant 和移除 `finalize_response` 以[综合技术方案](phase-one-agent-native-delivery.md)为准。

> 迁移翻译固定如下：旧二元确认中的“无需确认”映射为 `never`，“每次确认”映射为 `per_call`；只有用户显式创建受限 Grant 才使用 `session_grant`。旧的“新 Runner 读取 Skill latest”不得迁移，当前 AgentVersion/draft_test_snapshot 必须固定精确 SkillVersion/checksum。

本文记录V1 Tool代码注册、治理、执行信封、内置Tool契约和Skill包生命周期基线。

## 1. Tool代码注册

实现模块暴露 `ToolPlugin`：

```python
class ToolPlugin(Protocol):
    metadata: ToolMetadata
    async def execute(self, request: ToolRequest) -> ToolResultEnvelope: ...
    async def cancel(self, attempt: ToolAttemptRef) -> CancelResult: ...
    async def lookup(self, provider_request_id: str) -> ToolResultEnvelope: ...
```

`ToolMetadata`含 key、显示默认值、input/output JSON Schema、implementation_ref/version、execution_mode、side_effect、retry/cancel/status_lookup能力、可编辑platform config schema。服务启动执行 Registry Reconciler：代码是 key/schema/implementation/execution_mode权威；数据库保存治理状态。代码移除但仍有引用时标 registration_missing并拒绝执行，不删历史。

管理列表可编辑：display_name、description、enabled、confirmation_policy、实现声明可编辑的platform_config；不可编辑/创建：key、Schema、implementation_ref/version、execution_mode。更新config_version并写 `platform_audit_event` 治理事件；无 Session，不写 ExecutionEvent。

## 2. 调用与Attempt

模型Tool call经 canonical参数校验后先创建ToolInvocation。`inline`仅表示结果可在当前Runner及时返回，不允许跳过持久化；`durable_async`始终结束Runner。`execute`注册为durable_async并走sandbox topic。

ToolAttempt是物理调用；技术重试保持同Invocation、arguments_hash与business idempotency key。Attempt开始前再次检查Tool enabled、Confirmation与fence。取消尽力而为；有副作用且不确定结果时为`outcome_unknown`。

## 3. 统一信封

输入：

```json
{"invocation_id":"uuid","tool_key":"read_file","arguments":{},"idempotency_key":"...","principal":{"user_id":"..."},"context":{"session_id":"...","workspace_id":"..."}}
```

成功输出：

```json
{"status":"succeeded","data":{},"summary":"human safe summary","artifact_ids":[],"provider_request_id":null,"retryable":false}
```

错误输出：

```json
{"status":"failed","data":null,"error":{"code":"invalid_path","message":"...","category":"validation|authorization|transient|permanent|outcome_unknown","details":{}},"retryable":false,"artifact_ids":[]}
```

信封不得包含宿主机绝对路径、凭证或未脱敏堆栈。大输出写Workspace并返回Artifact/临时文件引用。模型只看到safe message、结构化data和Artifact ID。

## 4. 首批Tool契约

### `read_file`（inline、目标默认 `never`、Workspace read锁）

输入：`path`（Workspace相对路径或Skill Catalog只读URI）、可选`offset/limit/encoding`；输出 `content/truncated/next_offset/size/checksum`。拒绝绝对路径、`..`、越界软链接；Skill URI必须属于Runner固定checksum目录。最大返回由平台限制，超限要求分页。

### `write_file`（inline、目标默认 `per_call`、write锁）

输入：`path`、`content`或`source_artifact_id`、`mode=create|replace`、可选`expected_checksum`；输出相对路径、size、checksum。使用临时文件atomic rename；默认不发布Artifact。存在冲突或checksum不符返回version conflict，不静默覆盖。

### `execute`（durable_async、目标默认 `per_call`、Workspace独占锁）

输入：`argv: string[]`（禁止shell字符串拼接）、可选`cwd`相对路径、`timeout_seconds`、`env`白名单、`stdin`；输出exit_code、stdout/stderr截断摘要、output file列表、可选Artifact IDs。禁止注入平台秘密，限制输出/超时，取消尽力终止进程树。Local Sandbox不是不可信安全边界。

### `invoke_agent`（durable语义、目标默认 `never`）

输入：`target_agent_id`、`request`、可选`context`、`idempotency_key`；输出AgentInvocation最终信封。Chat/Plan严格星形，Group仅图内节点；继承原userId，目标使用自身装配。调用一定产生持久AgentInvocation与定向结果，不在Runner内轮询等待。

### `search_group_history`（inline只读、目标默认 `never`）

输入：`query`、可选`agent_ids/event_types/from_sequence/to_sequence/cursor/limit<=100`；必须有当前group/node。通过统一 `group_shared_search` 投影查询：可返回 group visibility，以及调用节点本身是recipient的direct记录；绝不返回其他节点direct或任何private记录。输出命中共享Message/Event/Artifact安全摘要、sequence、因果引用、`scanned_through_sequence/max_sequence`和next_cursor。排除Instructions、Memory、Prompt、推理、私有Tool数据、未发布文件；查询上界在调用开始固定，过滤后的sequence空洞合法。

## 5. Confirmation与治理

目标实现中，`enabled` 与 `confirmation_policy=never|per_call|session_grant` 由 PlatformTool 当前行决定，AgentVersion 不复制。默认 read/invoke/search=`never`，write/execute/副作用 Tool=`per_call`；`session_grant` 只在用户已有匹配范围的有效 Grant 时免去本次确认。批准绑定 tool_key+arguments_hash+principal_hash；重试不重复批准，参数变更创建新调用。即时变更语义见当前 [Tool 模块](../modules/tool-system.md)；本历史页的旧实现细节不能覆盖该规则。

## 6. Skill上传与发布

API只接受zip/tar.gz，大小/文件数/单文件限制由平台配置。流程：

1. 流式上传到临时目录并计算SHA-256；拒绝绝对路径、`..`、设备文件、越界链接和解压炸弹。
2. 解包并要求根目录存在`SKILL.md`；解析最低name/description，验证所有引用仍在包内。这里做结构校验，不做脚本安全判断。
3. 将原包原子移动至内容寻址package_uri，创建SkillVersion `uploaded→validating→published`；失败为rejected并保存错误。
4. 发布事务更新Skill.current_published_version_id并写 `runtime.maintenance:sync_skill`；包与版本不可变。

Skill名称冲突由调用方明确选择“给既有Skill发布新版本”或“创建新逻辑Skill”，不按name静默覆盖。

## 7. 共享Skill Workspace

Sync Manager将包解到临时 `<checksum>.tmp.<id>`，重新校验checksum/清单后chmod只读并atomic rename到`shared-skills/<skill-id>/<checksum>`。启动、发布、周期对账和管理员手动触发共用同一幂等接口。每checksum只落盘一次；当前版本别名只用于管理显示，Runner快照必须使用checksum实体路径。

历史 V1 曾在 Runner 启动时解析逻辑 Skill 的 latest published 版本。该行为已废弃：目标实现只读取 AgentVersion 或 draft_test_snapshot 固定的 `skill_id/version_id/checksum/path`，同一 WorkItem 的后续 Runner 也不得漂移。完整 SKILL.md 和引用内容仍通过显式 `read_file` 渐进加载；未装配 read_file 时只有 name/description/path Catalog，平台不自动添加 Tool。

Skill与Tool权限完全解耦：上传/发布不解析“依赖”来授权，不自动装配read_file/execute/MCP/Agent，不因脚本存在改变Sandbox。第一阶段脚本视为可信，但只有显式execute才能运行。
