# V1 代码审计与迁移计划（历史）

> **历史V1审计。** 本文保留旧代码处置证据；Agent-native v2的迁移顺序、Schema和完成门禁以[第一阶段综合技术方案](phase-one-agent-native-delivery.md)为准。

本文记录2026-07-31对仓库后端、前端、迁移、Docker与测试的现状审计，以及第一阶段实施处置；它不是产品权威模块，不表示实现已完成。

## 1. 审计结论

仓库约12.5k行应用/测试代码，具备可运行的FastAPI+SQLAlchemy Async+Alembic、React/Vite、Docker、Mock LLM和多层测试骨架。核心领域仍是旧设计：可变Agent大JSON、每次编辑即快照版本、数据库Tool+代码Builtin双体系、提示词式Skill、MCP、Task/Celery、Redis Pub/Sub/WebSocket、Span兼状态记录、Plan JSON+Task串行执行、Group Task DAG/Manifest/Event Bus、Fork。

没有发现`Message/Run/WorkItem/ToolInvocation/AgentInvocation/Clarification/ExecutionEvent/Checkpoint/Outbox/SessionSandbox/Artifact/GroupSession`等第一阶段权威实体。现有系统无法通过局部加字段达到等待恢复、至少一次调度、fencing和冻结图语义，核心模型与Runtime必须换代式重建。

粗略按可复用工程价值而非LOC：后端约35%保留/小改、65%大改/替换；前端约25%保留/小改、75%大改/替换；测试基础约45%保留、55%重写用例。全仓综合约三分之一复用、三分之二换代，不建议删库重搭仓库。

## 2. 代码处置矩阵

| 范围 | 处置 | 现状与目标 |
| --- | --- | --- |
| `backend/app/main.py`、API router | 小幅改造 | 保留FastAPI/lifespan/CORS；WebSocket/Redis事件换SSE事件服务 |
| `config.py/database.py/deps.py/security.py` | 小幅改造 | 保留Async基础/JWT/bcrypt；增加userId、三角色、UoW、credential_ref、事务隔离 |
| `core/model_hub/*` | 小幅改造 | OpenAI/Anthropic适配器可复用；改无副作用params、统一stream/usage/error、Registry Model |
| Agent model/service/schema/API | 替换 | 可变大JSON和编辑即版本改为稳定Agent+draft/publish+关系装配 |
| Tool DB model/service与Builtin registry | 替换 | 合并成代码注册+数据库治理的唯一PlatformTool |
| Skill model/service/page | 替换 | Text Prompt改为包、SkillVersion、checksum和共享同步 |
| MCP model/API/client/page | 删除或后置 | 第一阶段不进入核心 |
| Session/Runner/Span | 大幅改造 | Session壳可借鉴；新增Message/Run/WorkItem/Checkpoint/Event，Span只观测 |
| Task model/API/service/Celery workers | 替换后删除 | WorkItem/Runner/Invocation+Database Outbox；Celery不作产品/队列核心 |
| `agent_runtime/context_builder` | 替换 | 旧同步循环与Prompt块不满足固定装配、Policy、持久等待 |
| `plan_executor`/ExecutionPlan | 替换 | JSON步骤+单Agent串行改关系化冻结WorkItem Graph、固定版本、partial settlement |
| `group_orchestrator`/OrchestrationGraph | 替换 | 旧中心Task DAG改共享GroupSession/GroupRun/Agent节点 |
| memory manager | 替换 | Redis短期+宽scope改四Policy与可审计事实Memory |
| event bus/Capability/Event Manifest | 删除或后置 | 不得形成隐藏Agent通道；ExecutionEvent不是广播订阅总线 |
| channels/notification | 大幅改造/后置 | adapter可参考；改Channel绑定、Session来源、NotificationDelivery Outbox |
| migrations 0001-0003 | 保留历史、开发重建 | 已确认无须保留生产数据；采用新Schema基线，不在本任务执行drop/migration |
| React/Vite/Router/Query/Zustand/UI | 保留 | 工程与组件基础继续使用 |
| Agent Config/Session/Plan/Group pages | 替换 | 按[前端设计](frontend.md) |
| Admin表格/登录/布局 | 小幅或大幅改造 | 复用外壳，换资源契约与角色可见性 |
| Dockerfile/Compose | 大幅改造 | 保留镜像基础/MySQL；删除Celery队列，Redis仅作为必需非权威Token Bridge，新增多Topic Worker、共享卷/maintenance |
| Mock LLM/测试脚本/CI | 保留并扩展 | 保留分层与真实部署方法；fixture/schema/断言重写 |
| 旧Task/Plan/Fork/MCP测试 | 删除或替换 | 不可成为新行为回归标准 |

## 3. Schema迁移建议

负责人已于2026-07-31确认不存在必须保留的生产数据。实施阶段采用完整的第一阶段Schema新基线并重建开发/测试数据库，不在旧三版表上连续修补。旧Alembic文件保留在归档branch或标记legacy；新基线必须能在空库一键创建并有downgrade边界。该决定不授权在本设计任务中执行drop、迁移或数据库重建。

## 4. 九阶段实施

### 1. 基础Schema与Registry

交付：公共类型/UoW、账号userId/角色、Model/Tool/Skill/Policy/Sandbox Registry、ExecutionEvent/PlatformAuditEvent/Outbox基础、事件可见性投影、代码Tool reconcile、空库migration。

验收：空库升级；Registry seed幂等；执行状态+ExecutionEvent+Outbox和管理状态+Audit+Outbox分别原子回滚；审计不占Session sequence；事件dedupe允许不同cause；角色/用户隔离；Tool代码字段不可由API改。测试：migration、trigger/generated-column/nullable-unique约束、projection安全、repository、权限集成、并发唯一键、Docker smoke。

### 2. Agent装配和发布

交付：Agent/draft/publish/immutable version、关系装配、五区API和页面、API Key生命周期。

验收：不完整草稿可存但不能发布；publish复制且draft继续存在；published/test snapshot触发器不可变；并发发布只分配一个version_no；新发布不改历史引用；private/enterprise与builder边界正确。测试：领域状态机、发布事务故障回滚、trigger、冲突、Draft Test固定、API契约、Playwright配置/发布。

### 3. Chat垂直闭环

交付：UserSession/Message/Run/WorkItem/Runner、DatabaseOutbox runtime worker、Outbox/Runner lease切割、Assembly/Prompt/Model流、Redis Token Bridge、可见性过滤SSE、最终Message。

验收：202后断开仍完成；Redis/API重启只丢delta不丢Run；新Run用最新AgentVersion；无fallback；重复请求不重复Run；SSE过滤空洞仍推进cursor。测试：状态/事务、六个Outbox/lease崩溃窗口、adapter contract、Redis故障、Mock LLM全栈、SSE visibility/chaos、UI Chat。

### 4. Tool、Skill、Sandbox、Artifact

交付：Invocation/Attempt/Confirmation、五个最低Tool、Skill包/同步/checksum、SessionSandbox/Workspace锁、Artifact。

验收：Tool未装配不可调用；策略即时生效；Skill新Runner热更新；路径越界拒绝；execute独占；Artifact版本不可覆盖；MemoryHead保证同逻辑键一个active且用户编辑胜过陈旧candidate。测试：Tool contract/idempotency/outcome_unknown、Memory并发/删除召回、包攻击样例、锁并发、文件集成、UI管理/确认/Artifact/Memory。

### 5. 持久化等待与恢复

交付：Clarification、durable Tool callback、Checkpoint、lease/heartbeat/fencing、取消/超时/迟到结果、control worker。

验收：在领取、模型后、Tool受理、等待阶段杀Worker均不重复推进；僵尸写被拒；用户离线可恢复。测试：故障注入、property/state machine、并发取消完成、callback重复、长等待E2E。

### 6. Plan

交付：关系化Proposal/图编辑/确认冻结、固定AgentVersion、星形AgentInvocation、依赖调度、partial settlement与后继Plan。

验收：图确认前可改、后不可改；并行all-settled；子Agent不能再调用；停用/新版本语义符合权威文档。测试：DAG校验/调度、冻结并发、权限、部分失败、Worker恢复、React Flow E2E。

### 7. 最小Group

交付：关系化Formation Proposal/immutable revision/Bootstrap来源/参与者/初始WorkItem/建议边、固定节点、GroupRun版本映射、共享Message/Event、定向投影/消息/Clarification、图内互调与实际边、search_group_history、退出/取消/关闭。

验收：revision CAS且确认后冻结；建议边不伪造实际调用；完整Chat/direct/private不泄露；未接收节点不唤醒；图外调用拒绝；终态追问创建新Run；固定的unavailable节点不被替换/删除；非owner不可用不阻塞其他节点；owner不可用必须改选可用节点；Group关闭只读。测试：Proposal关系约束/编辑并发/确认原子性、投影安全与sequence空洞、并发节点/Workspace、定向交互、版本固定、Group UI全栈，以及非owner停用仍可创建、默认owner停用409、显式改选owner成功、unavailable目标调用拒绝、重新启用仅影响未来GroupRun等状态/竞态用例。

### 8. 可观测性、权限和完整验收

交付：OTel Trace/Span Link、Timeline、Platform Audit管理页、NotificationDelivery/Attempt与Channel Profile delivery-worker、限流/保留、全场景验收和运维手册。

验收：Trace/Notification失败不影响Run；Web/API不创建虚假Delivery；Channel投递幂等/重试/迟到结果明确；Builder/Admin不越权读内容；七个产品验收场景通过；恢复/重复投递可证明。测试：Channel provider contract与故障注入、安全负面、敏感信息扫描、审计访问、负载/积压、Trace链、全栈/Playwright、备份恢复演练。

### 9. 删除旧实现

交付：删除Task/Celery产品模型、双Tool、提示词Skill、MCP入口、旧Group DAG/Event Manifest/Fork及兼容代码；更新Compose/CI/文档。

验收：`rg`不再发现旧路由/表/导航的运行引用；Celery不再存在，Redis故障时Run仍由MySQL可靠完成且只降级实时Token体验；所有新测试通过。测试：依赖/死代码检查、空库安装、升级文档演练、完整回归。

## 5. 实施门禁与首步

第一实施步骤不是先写Runtime，而是建立“新Schema基线 + UoW/Event/Outbox原子提交 + Registry reconcile”的可执行骨架。该切片决定后续所有状态机能否可靠落地。开发/测试数据库重建仅发生在后续实现任务，并由该任务显式执行，不属于本文档变更。

## 6. 已确认实施前提与后置事项

- **已确认：当前不存在必须保留的生产数据。** 新Schema基线和开发/测试环境重建方案不再受数据迁移兼容约束。
- 当前没有产品权威文档之间的直接矛盾。固定 Group 的节点停用语义已由用户于 2026-07-31 确认并写入跨模块不变量，第一阶段不再有产品确认阻塞项。MQ产品、Kubernetes、MCP、动态审批、Fork等继续保持后置，不在实现阶段擅自选型。
