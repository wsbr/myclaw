# UAT 运行时工具统一绑定管理

已部署到 UAT，release 为 `uat-runtime-tools-20260922-61de03f4f075276e`。工具目录包含全部 11 个协调工具；其中 10 个是新增目录项，`search_group_history` 升级原目录项。

## 变更

将原先由 Runtime 自动注入的 11 个工具纳入现有工具目录、不可变 ToolVersion、Agent 草稿绑定与发布流程：

| 工具 | 用途 | 适用模式 |
| --- | --- | --- |
| `search_tools` | 查找已绑定的工具 | 对话、规划、群组 |
| `update_plan` | 更新工作计划 | 对话、规划、群组 |
| `find_agent_user` | 查找授权范围内用户 | 对话、规划、群组 |
| `ask_user` | 发起持久化澄清 | 对话、规划、群组 |
| `discover_agents` | 发现可协作 Agent | 对话、规划、群组 |
| `select_group_members` | 为后续群组选择成员 | 根对话 |
| `invoke_agent` | 委派任务 | 对话、规划、群组 |
| `search_group_history` | 查询群组可见历史 | 群组 |
| `send_group_message` | 发送群组消息 | 群组 |
| `invite_agent` | 请求邀请成员 | 群组，仍受固定成员策略限制 |
| `update_shared_task` | 更新共享任务 | 群组 |

工具管理页显示“内置会话与协作”和适用模式；Agent 的“能力装配”使用同一工具选择器。平台启停或目录刷新后，绑定选项会同步更新。

## 执行约束

- 未绑定的工具不会出现在有效集合，也不能调用；启用工具本身不授予任何 Agent 权限。
- 绑定冻结 ToolVersion 和内容哈希；执行时检查真实 Agent、Runner、能力快照、治理和模式，不凭 `source` 或注解授权。
- 原 `search_group_history` 复用同一 ToolDefinition ID；旧发布版本保留原冻结合同。
- 协调调用统一记录 ToolInvocation、执行幂等和预算。用户澄清、Agent 委派仍由领域结果恢复，受理回执不会覆盖最终结果。
- `plan_readonly` 允许已绑定的 `update_plan` 和 `ask_user`；业务写入仍被阻止。Plan 缺少有效 `update_plan` 时明确报错，避免循环重试。
- 未绑定 `search_tools` 时直接呈现其他有效的已绑定工具，避免出现无法发现的授权能力。
- 本轮协调工具仅支持 `confirmation_policy=never`。UI 限制选择，API 对不支持的执行确认策略返回 422；成员、用户范围及领域审批仍生效。

## 已有 Agent 的使用方式

到 Agent 配置 → 能力装配，按职责选择所需工具，保存草稿并发布。例如规划需要 `update_plan`，持久化提问需要 `ask_user`，委派需要 `invoke_agent`。Group 开关和可调用 Agent 范围不代替工具绑定。

本次不会自动选择或发布任何 Agent 的工具，也不会改写历史发布版本、草稿绑定和账户配置。升级后，旧版本中未显式绑定的协调能力不再可用。

## 验证

- 后端完整单元测试：518 通过。
- 前端测试：18 通过；TypeScript 和 Vite 构建通过。
- 真实 MySQL：3 个集成测试通过，包含注册幂等、旧绑定保留、显式绑定后发布、真实搜索/更新计划/提问、跨连接幂等重放、预算拒绝、工具合同和用量账本。
- 真实调用使用隔离测试库，未调用付费模型；FK 和不可变触发器保持开启。
- 从最终后端镜像提取实际部署源码，另跑 47 个授权、恢复、目录及真实 MySQL 专项测试，全部通过。
- 文档链接检查及 `git diff --check` 通过。

## 发布核验

- API、前端健康检查和工具页路由均为 HTTP 200；10 个 Worker 进程心跳正常。
- 11 个后端容器的 11 个变更文件逐一核对哈希；前端镜像 ID 与构建记录一致，启动日志未发现异常。
- 发布前后核对 Agent、AgentVersion、AgentVersionTool、Run、ToolInvocation 全表摘要一致；原有工具 ID、启停、确认策略及治理版本保持不变。
- 继承前一轮基础修复镜像，保留 UAT 既有上下文实现和前端预设；本次无需数据库迁移，schema 仍为 0051。
- 未执行登录后的浏览器冒烟，当前浏览器无已认证会话；工具目录和绑定通过数据库、API 集成测试及最终镜像验证。

部署证据、精确文件哈希、构建日志与回滚配置保存在 `deploy/uat/evidence/runtime-tools-20260922/`。数据库备份为 `deploy/uat/backups/myclaw-uat-20260922T071735Z.sql.gz`。
