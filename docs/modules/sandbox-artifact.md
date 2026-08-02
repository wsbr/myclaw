# Sandbox、Workspace 与 Artifact

本文是 Sandbox 装配、Session Workspace、文件并发、回收策略和轻量 Artifact 的唯一权威来源。

## Sandbox Registry 与 Agent 装配

Sandbox 是 Agent 显式选择的平台运行时能力。Agent 保存：

```text
sandbox_profile_id
```

平台维护 Sandbox Profile Registry。Agent 页面只选择 Profile，不配置宿主机路径、容器参数、挂载规则或资源实现。

第一阶段只注册：

```text
key: local
type: local
description: 物理机上的会话级目录隔离沙箱
```

`remote` 和 `aio` 只保留 Provider 扩展方向，具体协议和安全能力后置设计。请求和会话运行过程中不能临时覆盖 Sandbox。

## Session Sandbox

Session 创建时根据入口 Agent 当前 `sandbox_profile_id` 创建并固定 `SessionSandbox`：

```text
Agent Sandbox Profile
→ SessionSandbox
└── Session Workspace
```

Agent 后续修改 Sandbox 配置只影响新 Session。旧 Session 保持原 Profile，避免在存在文件、Artifact 和执行状态时静默迁移环境。

Chat、Plan 和从该 Session 创建的 Group 共用同一个 Session Sandbox。Runner 只挂载既有 Sandbox，不复制 Workspace；Runner、Plan 或 GroupRun 结束都不清理 Session 文件。

第一阶段所有 Agent 都使用 `local`，因此 Group 自然共享本地 Workspace。后续若 Group Agent 的 Sandbox Profile 不兼容，在统一 Workspace 挂载协议实现前必须阻止组图，不能静默忽略 Agent 配置或 fallback。

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

Skill 上传阶段的风险评估、信任等级和更强 Sandbox 兼容检查属于后期治理能力。

## Workspace 并发

Session Workspace 是共享可变资源。第一阶段对整个 Workspace 使用平台管理的读写锁：

- 多个只读文件操作可以并行；
- 任何写操作获取独占锁；
- `execute` 无法预知写入范围，执行期间获取整个 Workspace 独占锁；
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

用户明确删除 Session 或后期 Retention Policy 命中时，平台可以清理 Workspace，并同步将相关 Artifact 标记为不可用。清理必须有持久化记录，不能静默发生。

## 轻量 Artifact

Artifact 是 Session Workspace 中文件的正式登记记录，不是另一套文件系统：

```text
Artifact
├── artifact_id
├── userId
├── session_id
├── relative_path
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

文件本体仍由 Session Sandbox 管理。第一阶段不建设独立 Artifact Store，也不把普通消息、Tool JSON 结果或业务数据库对象泛化为 Artifact。

中间文件可以只存在于 Workspace。只有 Agent 或 Tool 显式发布的正式文件才登记为 Artifact，并通过 `artifact_id` 在前端、API、Agent 和 Tool 之间稳定引用，不能向调用方暴露服务器绝对路径。

已发布 Artifact 不可覆盖。修改时读取或复制到可变 Workspace，完成后发布新 Artifact，并以 `parent_artifact_id` 形成版本关系。平台不自动合并 Excel、PDF、图片等冲突版本。

Artifact 权限继承 `userId + session_id`。Group 中发布到 Group 的 Artifact 可被成员通过共享记录显式查询；未发布文件不会因为共享 Sandbox 自动进入其他 Agent Prompt。
