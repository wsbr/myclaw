# Skill

本文是 Skill 包格式、注册、共享同步、渐进加载和版本语义的唯一权威来源。

## 定义与兼容目标

Skill 是兼容通用 `SKILL.md` 结构的完整、可移植能力包。来自开源生态的 Skill 上传到 MyClaw 后不应要求改写为平台专用格式。

一个包可以包含：

```text
SKILL.md
scripts/
references/
assets/
templates/
其他由 SKILL.md 引用的文件
```

平台最低索引要求仅为 `SKILL.md` 中的 `name` 和 `description`。其他元数据可以被保留，但不能成为通用 Skill 运行的强制前提。

## 存储模型

Skill 使用逻辑标识与不可变版本分离：

```text
Skill
├── id
├── name
├── description
└── current_version_id

SkillVersion
├── id
├── skill_id
├── package_uri
├── checksum
├── content_hash
├── manifest
├── package_size
├── archive_type
├── created_at
└── status
```

上传接受根目录包含 `SKILL.md` 的 `.skill`、`.zip`、`.tar.gz` 或 `.tgz` 包。YAML front matter 至少包含字符串 `name` 和 `description`；其他声明元数据原样规范化保存在 `manifest.declared_metadata`，但不能扩大 Tool、Agent 或数据权限。`SKILL.md` 有独立大小上限且不接受 YAML alias，避免元数据解析成为资源耗尽入口；大型资料应放入 `references/` 等按需文件。

包源通过 `SkillPackageStore` 抽象访问。`local` 适配器用于单机开发/UAT；多节点使用 S3 兼容对象存储，数据库只保存不可变 URI、checksum、manifest 和索引，不保存或逐次传输包体大字段。对象键包含 `skill_id + skill_version_id + checksum`，同一 SkillVersion 的对象地址和内容不可改写。

## 共享 Skill Workspace

后端维护一个服务级共享、只读的 Skill 工作空间，每个 checksum 只落盘一次：

```text
shared-skills/
└── <skill-id>/
    └── <checksum>/
        ├── SKILL.md
        └── ...
```

同步触发包括：

- 服务启动；
- Skill 发布；
- 周期性对账；
- 管理员手动同步。

发布事务写 `sync_skill` Outbox，由独立 `skill-sync-worker` 消费并预热缓存。每个真正读取 Skill 的 Tool 节点仍执行按需 `ensure`；因此轮询、启动预热和 Outbox 只优化延迟，不承担正确性。节点可以丢弃全部本地缓存，再从数据库固定的 `package_uri + checksum` 重建。

同步重新验证归档路径和文件类型、校验压缩包 SHA-256、计算解压树哈希，并在 checksum 级跨进程锁内通过临时目录和原子切换发布只读缓存。缓存损坏会重新拉取；对象包校验失败则拒绝读取。Session 不复制 Skill 文件；模型或脚本生成的产物必须写入 Session 工作区，不能修改共享 Skill。

## Runtime 加载

发布 `AgentVersion` 或创建不可变 `draft_test_snapshot` 时，平台为每个已装配 Skill 解析当前发布版本，并固定 `skill_id + skill_version_id + checksum`。Runner 启动时只按该快照解析只读路径，不再读取 Skill 的 current pointer。

初始 Prompt 的 Skill Catalog 只包含：

```text
name
description
path
```

Catalog 有独立的总 token 上限；当固定元数据本身超过上限时发布/运行失败，不允许静默省略已绑定 Skill。完整 `SKILL.md` 及其引用内容由 Agent 按需通过已装配的 `read_file` 渐进读取，并作为 Tool Result 返回，不能提升为 system role。`read_file` 首先校验 `RunnerSkillSnapshot`，再在当前执行节点按 checksum 自愈缓存；未绑定的 `skill://` URI 始终拒绝。平台不会因为 Agent 选择了 Skill 而自动添加 `read_file`；未选择该 Tool 时，Agent 只能使用初始 Catalog 信息。

Catalog 的名称、说明和路径作为结构化的 `assistant` 数据段呈现，包内元数据不能成为 system 指令。平台的加载指导独立生成：仅当本轮实际呈现 `read_file` 时要求按精确路径读取；否则明确只有目录信息可用，不要求调用缺失能力，也不能声称已读取完整 Skill。该指导在工具被停用或动态加载后随下一次模型请求更新。

## 更新语义

- 可编辑 Agent Draft 绑定逻辑 `skill_id`，便于 Builder 选择能力；Draft 页面应同时显示当前解析到的 SkillVersion。
- 发布 AgentVersion 时把每个逻辑 Skill 解析为精确 `skill_version_id + checksum`，发布事务失败时不得留下部分绑定。
- Skill 新版本发布不会改变任何已发布 AgentVersion，也不会改变其下一次 Runner 的行为。需要采用新版 Skill 时，Builder 必须重新测试并发布新的 AgentVersion。
- `draft_test_snapshot` 同样固定精确 SkillVersion，保证测试结果可复现；Draft 在下一次保存或测试时可以重新解析最新版。
- Runner 快照记录实际使用的 SkillVersion、checksum 和只读路径；Observability Trace 可以引用该快照，但不能把路径作为身份或权限。

## 脚本与权限

Skill 包允许携带脚本。当前阶段采用可信 Skill 模型，不在上传时判断脚本安全性、依赖完整性或业务合理性。

脚本能否执行取决于 Agent 是否显式装配 `execute` 以及所选 Sandbox。第一阶段所有 Skill 脚本默认可信，可以在 `local` Sandbox 中执行，以支持通用 Skill 和项目测试。Skill 加载本身不会启动脚本。

`execute.argv` 中只有完整、已冻结的 `skill://<skill-id>/<checksum>/<file>` 参数会被解析为本节点缓存路径；任意物理宿主路径不会获得访问权。Sandbox 把对应 checksum 根加入只读 Landlock 规则，当前工作目录和所有输出仍限制在 Session Workspace。建议通过显式解释器运行只读脚本，例如 `python skill://.../scripts/check.py`。

`local` 只承诺会话目录隔离与执行生命周期管理，不是不可信代码安全边界。完整规则见 [Sandbox、Workspace 与 Artifact](sandbox-artifact.md)。

`enterprise_production_v1` 不沿用上述可信假设。生产上传必须记录 source/provenance、生成内容 checksum、执行恶意文件/依赖/secret 扫描，并形成 `trusted|unknown|quarantined` 评估；只有受信来源或经人工批准的 unknown 包可发布。脚本只能在 `security_level=microvm` 的 SandboxProfileVersion 中执行，默认拒绝出站且不注入宿主 Secret。扫描规则变化增加 assessment revision；新准入可以阻止旧包的新执行，但不能改写 AgentVersion/SkillVersion 历史。具体隔离和事件处置见[生产参考架构](../technical/production-reference-architecture.md)与[生产运行手册](../operations/production-runbook.md)。

Skill 不声明或扩大 Tool、MCP、Agent 或数据权限。
