# Sandbox、Workspace 与 Artifact

本文是 Sandbox 装配、Session Workspace、文件并发、回收策略和轻量 Artifact 的唯一权威来源。

## Sandbox Registry 与 Agent 装配

Sandbox 是 Agent 显式选择的平台运行时能力。Agent Draft 选择：

```text
sandbox_profile_id
```

平台维护 Sandbox Profile 与不可变 SandboxProfileVersion。Agent 页面只选择逻辑 Profile，不配置宿主机路径、容器参数、挂载规则或资源实现；Publish/draft_test_snapshot 固定精确 `sandbox_profile_version_id + content_hash`。

第一阶段只注册：

```text
key: local
type: local
description: 物理机上的会话级目录隔离沙箱
```

企业生产必须注册：

```text
key: isolated_kata_v1
type: remote_isolated
description: 专用节点池上的 Kata/microVM 会话执行环境
```

`isolated_kata_v1` 的隔离、网络、凭证、资源和存储合同由[企业生产参考架构](../technical/production-reference-architecture.md)定义。实现可以更换满足同等 Conformance 的 Sandbox Provider，但不能 fallback 到普通容器或 `local`。请求和会话运行过程中不能临时覆盖 Sandbox。

## Session Sandbox

Session 创建时根据入口 AgentVersion 固定的 `sandbox_profile_id + sandbox_profile_version_id` 创建 `SessionSandbox`，并固定 Workspace provider、mount policy 与 protocol version：

```text
Agent SandboxProfileVersion
→ SessionSandbox (persistent workspace authority)
   └── Session Workspace

WorkItem AgentVersion SandboxProfileVersion
→ RunnerSandboxInstance (ephemeral execution isolation)
   └── authorized mount of the same SessionSandbox
```

SessionSandbox 不是一个永久运行的容器/VM；它是持久 Workspace、挂载协议和存储身份。每个需要执行环境的 Runner 按自身 AgentVersion 固定的 SandboxProfileVersion 启动临时 RunnerSandboxInstance，再通过 Broker 挂载获授权的逻辑根。第一阶段 `local` 可以在同一物理机实现该接口，生产必须使用隔离实例。

Agent 后续发布新的 SandboxProfileVersion 不改变既有 AgentVersion 或正在运行的实例。旧 Session 也不静默迁移 Workspace provider/protocol；新 WorkItem 的 SandboxProfileVersion 与该 Session 不兼容时明确拒绝并要求创建新 Session，不能忽略 Agent 配置或 fallback。

Chat、Plan 和从该 Session 创建的 Group 共用同一个 SessionSandbox/Workspace。Runner 实例挂载既有 Workspace，不复制持久文件；Runner、Plan 或 GroupRun 结束都不清理 Session 文件。

第一阶段所有 Agent 都使用 `local`，因此 Group 共享同一物理 Session Workspace，但不共享全部逻辑路径。每次文件 Tool 都携带不可伪造的 ExecutionPrincipal，并按以下命名空间强制授权：

```text
workspace/
├── agents/<agent_id>/...      # 当前 Agent 的私有 scratch；原用户可访问
├── shared/...                 # 当前 Group 明确共享的可变文件
└── .artifacts/<artifact_id>/  # 平台只写的不可变发布快照
```

路径名不是权限。File Tool 和 `execute` 的工作目录解析器必须同时校验 `tenant_id + account_id + session_id + agent_id + group_session_id + visibility`；一个 Group 成员不能通过猜测路径、软链接、`../` 或自定义 cwd 读取另一成员的私有根。未进入 Group 的执行不能访问 `shared/`。

若 Group Agent 的 SandboxProfileVersion 与 SessionSandbox 的 workspace/artifact protocol 或 mount policy 不兼容，必须拒绝创建相应 GroupRunSnapshot 并返回 `sandbox_profile_incompatible`；不能静默忽略 Agent 配置或 fallback。

共享可变 Workspace 是第一阶段单控制域优化，不是 Agent Network 的长期互操作前提。远程或更强隔离的成员应在各自私有 Workspace 工作，通过版本化 Artifact、文件引用和显式发布交换结果；Group 协议不能依赖所有 Agent 挂载同一文件系统。

## Local Sandbox 边界

`local` 在物理机上为 Session 建立独立目录，并提供路径校验、文件生命周期和执行管理。它不是运行不可信代码的安全边界。

第一阶段规则：

- Skill 上传、同步和读取不会自动执行脚本；
- Agent 只有显式装配 `execute` 才能运行 Skill 脚本；
- Skill 脚本默认视为可信，可以在 Local Sandbox 中执行；
- Platform File Tool 规范化路径并阻止 `../`、软链接等越出 Workspace 的访问；
- `execute` 设置工作目录、超时和输出限制，并在取消时尽力终止进程树；
- 平台不向脚本注入数据库凭证、模型密钥等宿主机秘密；
- 平台服务使用最低权限系统账号运行。

目录隔离不能阻止恶意本地进程读取宿主机可访问资源。因此权威安全声明是：

> Local Sandbox 是会话工作目录隔离和执行生命周期管理机制，不是不可信代码的安全边界。

在 `uat_single_node` 中，Skill 上传阶段的风险评估、信任等级和强 Sandbox 兼容检查不是功能验收前置；在 `enterprise_production_v1` 中它们是必须实现并取证的准入门禁，见 [Skill](skill-system.md) 和[生产就绪要求](../operations/production-readiness.md)。

## Workspace 并发

Session Workspace 是分区后的可变资源。第一阶段对实际访问的逻辑根使用平台管理的读写锁；无法可靠解析访问范围时升级为整个 Workspace 独占锁：

- 多个只读文件操作可以并行；
- 任何写操作获取独占锁；
- `execute` 默认只能挂载当前 Agent 私有根和显式授权的 shared 根；无法预知写入范围时，执行期间获取这些根或整个 Workspace 的独占锁；
- 等待锁的 ToolInvocation 状态为 `waiting_resource`；
- 锁只覆盖实际访问 Workspace 的 ToolInvocation，不覆盖模型推理、用户等待或整个 Runner；
- 锁使用租约，进程异常后可以回收；
- 队列按稳定顺序恢复，避免长期饥饿。

如果一组文件变化必须保持原子性，应封装在同一次 ToolInvocation 中。平台不允许 Agent 跨多轮模型调用长期持锁。

持久化异步 Tool 不在等待外部系统期间占用 Workspace 锁：

```text
获取锁并固化输入
→ 释放锁
→ 外部异步执行
→ 再次获取锁并写入结果
→ 释放锁
```

未来可以在不改变上层语义的情况下细化为目录锁或文件锁。

## Workspace 回收

Sandbox 回收由独立的平台 Retention Policy 管理，不属于 Agent 配置。

第一阶段默认策略是：

```text
session_lifetime
```

即默认不主动回收，Workspace 与 Session 同生命周期。用户离线、Runner 结束、Plan 完成、GroupRun 完成或 Group 归档都不触发清理。

在 `uat_single_node` 中，Artifact 本体也位于 SessionSandbox；用户明确删除 Session 或 Retention Policy 命中时可以清理 Workspace，并把受影响 Artifact 标记为不可用。清理必须有持久化记录，不能静默发生。

`enterprise_production_v1` 使用版本化平台 Retention：active Session 保留 Workspace；删除请求先撤销在线访问并进入 7 天可恢复隔离区，期满后清理 Workspace。Artifact 本体由独立 Artifact/Legal Hold 策略管理，清理 Workspace 不能自动删除已发布 Artifact。默认期限及删除证明见[生产就绪要求](../operations/production-readiness.md)。

## 轻量 Artifact

Artifact 是从可变 Workspace 发布出来的不可变文件快照。第一阶段仍使用同一受控文件系统，不建设独立对象存储，但 Artifact 本体不再指向可继续修改的源文件：

```text
Artifact
├── artifact_id
├── owner_account_id
├── session_id
├── source_relative_path
├── immutable_storage_key
├── name
├── mime_type
├── checksum
├── created_by_agent_id
├── work_item_id
├── runner_id
├── tool_invocation_id
├── parent_artifact_id
├── status
└── created_at
```

`publish_artifact` 先分配 artifact_id/平台只写 key 并创建不对普通列表可见的 `publishing` 记录，再在受控 Workspace lease 中读取源文件、复制到临时位置、计算并复核 checksum，最后用原子 rename/create-if-absent 形成 `.artifacts/<artifact_id>/` 快照，CAS 提交 `published` 和 Event。只有完整快照与元数据都成功才对外显示 `published`；崩溃后由 Reconciler 按 artifact_id/checksum 收敛，源文件之后被修改或删除都不能改变 Artifact 字节。

Artifact 本体仍由 Session Sandbox 管理。第一阶段不建设独立 Artifact Store，也不把普通消息、Tool JSON 结果或业务数据库对象泛化为 Artifact。普通 File Tool 和 Agent `execute` 不能写入 `.artifacts/`。

`enterprise_production_v1` 保持同一 Artifact 领域语义，但本体写入版本化对象存储，不依赖 Session Workspace 存续。对象 key、create-if-absent、checksum、发布 Reconciler、下载签名和恢复规则以[生产参考架构](../technical/production-reference-architecture.md)为准；切换存储后端不能改变 artifact_id、owner、visibility、provenance 或不可变性。

中间文件可以只存在于 Workspace。只有 Agent 或 Tool 显式发布的正式文件才登记为 Artifact，并通过 `artifact_id` 在前端、API、Agent 和 Tool 之间稳定引用，不能向调用方暴露服务器绝对路径。

已发布 Artifact 不可覆盖。修改时读取或复制到可变 Workspace，完成后发布新 Artifact，并以 `parent_artifact_id` 形成版本关系。用户删除只创建受审计意图；Legal Hold 校验通过后，Retention Service 先撤销在线可见性并标记 `unavailable(deletion_pending)`，默认 7 天内 owner 可取消删除，恢复窗口到期才删除本体并进入 `deleted`；强制 Retention 到期不可由用户取消。storage/checksum 故障恢复也只能从受信副本找回与原 checksum 完全相同的字节并重新验证对象锁。整个过程不能用新字节覆盖旧 `artifact_id`。平台不自动合并 Excel、PDF、图片等冲突版本。

Artifact 权限继承 `tenant_id + owner_account_id + session_id`，并保存 `visibility=private|direct|group|user`。Web/OIDC 先解析内部 account；第一阶段 Agent API 也必须先把 raw userId 解析为 account，不能把裸字符串作为 Artifact 外键。Group 中明确发布为 `group` 的 Artifact 可被成员通过共享记录查询；私有 Artifact、未发布文件和其他 Agent 的私有根不会因为共享物理 Sandbox 自动变为可见或进入 Prompt。
