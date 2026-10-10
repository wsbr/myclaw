# Agent 两种协作方式修复（2026-09-22）

本次按用户明确的两种协作方式修复调用、发现和自动组群边界。修复前证据见[协作审计报告](UAT_AGENT_COLLABORATION_AUDIT_2026-09-22.md)。

## 实现行为

| 配置与请求 | 修复后结果 |
| --- | --- |
| A 绑定 B、B 绑定 C，三者均关闭 Group | 允许 A → B → C；C 的结果先交给 B，B 整合后再交给 A |
| A 仅绑定 B，直接调用 C，且 A/C 不满足网络资格 | 拒绝；A 不继承 B 的绑定 |
| A、B 均开启 Group，但未互相绑定 | 在原始用户使用权限和目标治理允许时，可以网络发现与委派 |
| A 未开启 Group，也未绑定目标 | 拒绝网络发现、未绑定委派及自动建群 |
| A 开启 Group，用户普通对话需要协作 | A 按任务语义选择网络成员，Chat 提交后自动建立包含 A 的任务群，在群内继续交互与委派 |
| 目标已绑定，但用户无使用权、目标不可用或禁止调用 | 拒绝；绑定不绕过公共授权检查 |
| 当前运行的 Group 邀请新成员 | 保持 `fixed_for_run` 拒绝；后续 Chat 可以重新选择下一轮成员 |

显式 Agent 绑定和双方网络资格是两条可以共存的调用路径。二者都保留原始用户身份、发布状态、可用性、权限和预算约束。具体协调操作仍须有已发布的工具绑定，开关或 Agent 绑定不会隐式授予工具。

## 主要变更

- [委派服务](../backend/app/services/agent_invocation_service.py)：移除 Chat/Plan 对子 Agent 再委派的一概禁止；逐跳检查调用者冻结版本的 Agent 绑定，或双方网络资格；使用可信 `Run.account_id` 检查每个目标。审计记录 `bound_agent`、`agent_network` 或 `group_membership` 路径。
- [运行预算](../backend/app/services/run_reliability_service.py)：新 Chat/Plan Run 默认最大委派深度由 1 调整为 8；全链共享调用次数、时间和其他运行预算，并检查循环和单工作项扇出。已冻结的历史预算不被改写。
- [Agent 目录](../backend/app/services/agent_directory_service.py)：网络发现必须由已入网的冻结 Agent 版本发起，只返回已入网且授权可用的目标。显式绑定目录独立解析，每个子 Agent 只得到自己的绑定目标。
- [能力快照](../backend/app/runtime/snapshot_resolver.py)和[Worker](../backend/app/workers/runtime_worker.py)：关闭 Group 时不暴露发现、自动选群能力；没有有效委派工具时不投影可调用对象。固定目标的名称、描述和 ID 提供给当前 Agent，支持多级任务路由。
- [组群服务](../backend/app/services/group_collaboration_service.py)和[协调工具](../backend/app/runtime/coordination_tools.py)：选群和 Chat 后形成群均核验发起者冻结版本、当前发布和治理。A 必须进入本次任务群，不能关闭 Group 后为其他 Agent 拉群，也不能被其他成员替代。
- [网络 API](../backend/app/api/agent_network.py)：网络列表默认只展示开启 Group 的 Agent，按 ID 解析也要求目标参与网络。
- 协作工具说明、Agent 配置与网络页面、六份模块文档已统一这两种语义。

## 版本与配置边界

发布新 Agent 配置只影响后续创建的 Runner；已开始的 Runner 继续使用自己的冻结 AgentVersion。旧关闭版本不会因为新版开启 Group 而获得网络能力。已开启的旧 Runner 保留冻结能力，但若发起者发布版本在选群与形成之间变化，本次自动建群会安全跳过，需要在新运行中重新选择。

本次不会自动改写或重新发布已有 Agent，也不会补充工具绑定。固定链的每个调用者应绑定 `invoke_agent`；需要网络搜索或自动组群的 Agent 按职责绑定 `discover_agents`、`select_group_members` 及实际使用的群内协作工具。

## 验证与发布记录

- 全量后端单元回归：585 个通过。
- 从实际构建镜像导出的代码执行协作、工具授权、组群和预算测试：124 个通过。
- 前端单元测试：18 个通过；类型检查、Vite 生产构建和容器构建通过。
- 从实际构建镜像导出的代码执行真实 MySQL 集成：4 个通过（逐跳 Chat、逐跳 Plan、自动 Group 协作、工具目录与冻结绑定回归）。外键及不可变触发器保持开启，验证中没有 ProviderAttempt 或付费模型调用。
- UAT 已于台北时间 2026-09-22 16:36 完成部署并核验。发布号：`uat-agent-collaboration-20260922-7279ed347a9ad6d3`。
- 12 个应用容器使用预期镜像；11 个后端容器的 9 个变更文件和 3 个继承上下文文件哈希一致。10 个 Worker 心跳正常，API/前端健康，启动错误为 0。
- 9 张业务及配置表的部署前后行数、完整内容摘要一致，包括 139 个 AgentVersion、1618 条工具绑定、19 条 Agent 绑定。只有 3 个协作工具因说明更新生成新目录版本，已有冻结绑定没有切换。
- 页面路由仅验证未登录页面可达；没有进行登录后的浏览器操作或真实模型端到端运行。协作行为由实际服务、Dispatcher、Worker 结果恢复及真实 MySQL 测试覆盖。

发布构建保留上两轮基础设施和工具绑定修复，以及 UAT 原有的上下文配置差异。没有新增数据库迁移，Schema 仍为 `0051`；前后摘要确认已有发布版本、工具绑定、Agent 绑定和历史运行事实保持不变。

回滚镜像为上一轮 `myclaw-uat-runtime-tools:61de03f4f075276e` 与 `myclaw-uat-runtime-tools-frontend:1aefc53d7a22f27d`。部署前备份：`deploy/uat/backups/myclaw-uat-20260922T083240Z.sql.gz`，已校验 gzip 完整性。

本轮构建与核验材料位于 `deploy/uat/evidence/agent-collaboration-20260922/`（本地忽略目录），包含固定源码、构建镜像指纹、前后状态摘要、测试日志、数据库备份引用及回滚配置。
