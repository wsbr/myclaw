# MyClaw 整体评估与优化方案

评估日期：2026-09-16 至 2026-09-17。工作分支：`gpt-6`。

基线为 `83c98ad2979423974d22825e925a183ba52b8fbb` 加评估开始时的工作树：22 个已修改文件、4 个未跟踪文件。原有改动包含 Schedule 定义引用、Provider/Tool 参数处理、迁移 `0049` 和 Runtime 演进文档，均保留。下述发现与测试数字记录评估时点，不代表所有问题已修复。

后续经确认，2026-09-17 已实施上下文首批优化：按 Agent 装配策略方案、版本化参数发布、有界增量摘要与缺口记录、统一请求预算、每轮实际上下文清单及异步恢复边界修正。实施范围与限制见 [Runtime 演进实施记录](RUNTIME_EVOLUTION.md#e-2026-09-17-上下文首批实施)及[上下文策略契约](modules/context-memory.md)。其余评估项仍按下述优先级推进。

## 1. 总体判断

**项目已形成有价值的持久 Agent Runtime 原型，下一阶段应从扩充功能转向执行正确性、用户数据可信度和可验证交付。**

Chat、Plan、Group 共用模型循环；Run / WorkItem / Runner、Outbox、Checkpoint、lease/epoch、权限快照和引用证据已经有实质实现。知识检索、调度、成本账本、评测与管理界面也形成了完整链路。适合继续迭代受控单机/UAT产品。

当前主要问题出现在模块交界处：取消与行动提交、摘要与历史裁剪、分页 API 与页面状态、登录身份与客户端缓存、数据库与文件备份。单测和构建通过，并未覆盖这些边界。应先修复可观察的错误，再开展保持行为的结构重构。

| 维度 | 当前评价 | 下一步重点 |
| --- | --- | --- |
| 产品定位 | 可治理、可恢复的 Agent 工作网定位明确；业务价值仍待实测 | 选择一个真实任务，比较单 Agent、委托与 Group 的结果、时间和成本 |
| 后端架构 | 统一循环与持久事实源值得保留 | 把执行所有权检查推进到每次行动写入边界 |
| 前端体验 | 核心流程齐全，已有 SSE 恢复机制 | 修复分页与账号缓存，再收敛实时数据更新 |
| 上下文/知识 | 有摘要、预算、混合检索与精确引用 | 修复摘要停滞、实际请求审计，以及跨网络事务 |
| 工程验证 | 260 个后端单测通过，已有数据库/真实模型验收资产 | 将数据库与前端交互门禁真正接入 CI |
| 运维安全 | 有租户校验、加密、隔离选项和恢复脚本 | 修复已确认边界问题；恢复证据必须跟随版本更新 |

保留现有模块化单体和独立 Worker 部署方式。现阶段的主投入不应是微服务拆分、新的编排框架或更多协议适配。现有 [Runtime 演进方案](RUNTIME_EVOLUTION.md)中的统一预算、领域事件投影和按职责拆分方向合理；本评估补充全项目风险、复现证据和实施顺序。

## 2. 评估范围与验证结果

代码范围包括后端 API、Runtime、持久化、检索、鉴权、调度、前端会话与登录流程、Compose、CI、备份恢复和产品文档。规模约为后端应用 169 个文件、前端源码 58 个文件，合计约 5.55 万行；迁移文件 49 个。规模仅用于理解维护成本，不作为质量评分。

| 检查 | 本次结果 | 能证明的范围 |
| --- | --- | --- |
| `PYTHONPATH=backend test/.venv/bin/pytest -q test/unit/backend` | **260 passed，13.29 秒** | 当前工作树的既有确定性后端单测通过 |
| 前端 `tsc --noEmit` | **通过** | 类型检查通过 |
| 前端 Vite production build | **通过**；5,037 modules；最大 JS chunk 337.73 KB / gzip 115.68 KB | 生产产物可构建；使用已有 Node runtime，产物输出临时目录 |
| `scripts/check_compose_configs.sh` | **通过** | 脚本列出的 Compose 配置均可解析 |
| `scripts/check_documentation.sh` | **通过** | 已跟踪 Markdown 的本地链接有效 |
| `git diff --check` | **通过** | 补丁空白格式检查通过 |
| Alembic `heads` | **0049，单 head** | 迁移图可加载；不等于迁移 SQL 已在 MySQL 执行 |
| 合成输入的内存复现 | 见 F01、F04、F05、F06、F08，以及第 4 节 production 配置校验 | 验证局部行为，无真实凭证、模型调用或外部写入 |

本次未启动或改动部署、执行 MySQL 升降级、恢复演练、真实模型验收、浏览器全栈验收或性能压测；未进行在线依赖漏洞审计。工作树未提交，因此这些结果不是绑定干净 release commit 的发布证据。

## 3. 优先修复的具体问题

P1 表示影响现有执行、数据或访问边界，应进入下一轮修复；P2 表示可诊断性、容量、维护或条件性的生产门禁问题。证据区分局部复现、调用链确认与待测量风险。

### F01 · P1：旧 Runner 在取消或失去所有权后仍可提交行动

- **证据**：[ActionDispatcher](../backend/app/runtime/action_dispatcher.py) 第 760–802 行仅锁住 Session，随后进入协调工具；[WorkingPlan 服务](../backend/app/services/working_plan_service.py) 第 83–87 行只验证 Run 和 Session；[Runtime Worker](../backend/app/workers/runtime_worker.py) 第 1243–1269 行在最终 settlement 的 fencing 检查前已经提交循环事务。
- **触发与影响**：模型调用期间 Run 被取消或 Runner 被替换，旧响应迟到；inline 行动仍可能修改计划等业务事实。最终结算拒绝旧 Runner 无法撤销先前已提交的行动。
- **验证**：内存桩通过真实 Dispatcher → `update_plan` → `update_working_plan`，传入 cancelled Run 与 stale Runner，返回 succeeded 并创建计划记录。已核对上层调用及提交顺序；尚未做真实数据库并发故障注入。
- **方案**：统一执行所有权检查，在行动的短写事务中校验 Run 可执行状态、WorkItem.active_runner_id、execution_epoch 和 Runner lease；模型调用前另做快速检查。heartbeat 失效应通知执行任务停止。迟到结果只进入受控事实记录，不产生新的业务行动。
- **验收**：取消、旧 epoch、旧 active_runner 三类情况下，迟到响应均不能新增 Tool/Agent 行动或计划更新；重复恢复不产生第二份副作用。检查与写入须在同一事务/锁顺序内，避免先查后写的竞态。

### F02 · P1：浏览器切换账号未清除业务缓存

- **证据**：[main.tsx](../frontend/src/main.tsx) 第 10–16 行持有全局 QueryClient，staleTime 为 30 秒；[auth store](../frontend/src/store/auth.ts) 第 33–35 行只清 token；[WorkspaceLayout](../frontend/src/components/layout/WorkspaceLayout.tsx) 第 220–222 行采用 SPA 跳转；Query key 未统一包含 tenant/account。
- **触发与影响**：A 登出后 B 在同一页面登录，可能直接展示 A 的智能体、会话或通知缓存。即使缓存过期，刷新前也可能先展示旧值。后端鉴权无法清除已存在浏览器内存中的数据。
- **方案**：身份变化时取消在途请求、停止流订阅、清理查询缓存；建立含 tenant/account 的 query-key factory，并防止旧请求回写新账号的数据区。
- **验收**：同浏览器 A→B、B→A 切换，在新请求返回前后均不显示上一账号数据；慢请求在退出后返回也不能污染缓存。此项基于代码生命周期确认，本次未运行浏览器复现。

### F03 · P1：会话分页契约丢失，长对话看不到最新消息

- **证据**：[前端 Sessions API](../frontend/src/api/sessions.ts) 第 434、440 行只取 `items`，忽略 `next_cursor`；[后端 Sessions API](../backend/app/api/sessions.py) 第 206–245 行按时间升序返回默认前 100 条消息。会话列表默认 50 条，页面再按 Agent 过滤。
- **触发与影响**：超过 100 条消息后，新消息即使临时追加，refetch 仍会覆盖成最早 100 条；页面还可能从旧消息推导旧 Run。超过 50 个会话时，部分历史无法找到。
- **方案**：端到端保留 `Page<T>`；消息默认获取最新页并向上加载历史；会话列表使用服务端 Agent 筛选和游标分页；增量消息按稳定 ID 合并。
- **验收**：至少 101 条消息、51 个跨 Agent 会话，刷新与断线重连后最新消息和当前 Run 均正确。基于前后端契约确认，尚未执行浏览器场景。

### F04 · P1：登录限流可通过变换无效 Authorization 绕过

- **证据**：[main.py](../backend/app/main.py) 第 158–161 行把非空 Authorization 当作已认证；[rate_limit.py](../backend/app/core/rate_limit.py) 第 32–42 行按整个头字符串分桶；[登录接口](../backend/app/api/auth.py) 第 76 行不验证这个头。
- **验证**：同一合成客户端、每窗口限额 1，换三个假 Bearer 产生三个桶，三次请求均获准。该复现验证分桶逻辑，没有向部署发送请求。
- **方案**：登录使用可信来源地址基础桶及账号维度限流；身份额度只能绑定已验证主体，不能绑定任意请求头。明确可信代理边界和限流依赖异常策略。
- **验收**：随机更换无效头仍触发同一登录限流；合法账号/API key 的额度按稳定身份计算。

### F05 · P1：已有摘要后不再继续摘要，长会话出现覆盖空档

- **证据**：[history_service.py](../backend/app/services/history_service.py) 第 158 行接受有效旧摘要，第 166 行裁近期窗口，第 171 行仅在 `ready is None` 时创建新摘要。
- **验证**：10 条消息，旧摘要覆盖 m0/m1，近期窗口只容纳 m8/m9；返回 `trimmed_count=6`，没有新摘要排队。m2–m7 既不在摘要中，也不在实际历史窗口中。
- **方案**：已有摘要仍需依据未覆盖历史量触发推进；新摘要使用已验证摘要加后续增量，或有界重算，记录准确覆盖边界与 source hash。保持 Group 成员可见性隔离。
- **验收**：长会话连续触发多次摘要，覆盖边界向前推进且无重复排队；失败可以显式退化。应保证已成功摘要的覆盖连续，不承诺所有旧闲聊永久无损保留。

### F06 · P2：上下文审计清单与实际发送内容不一致

- **证据**：[context_assembler.py](../backend/app/runtime/context_assembler.py) 第 154–169 行删除历史却返回原 manifest；第 197 行精确计数裁剪也复用原 manifest。
- **验证**：构造 3 条历史并压低预算，组装结果包含 2 条消息、剩余 history=0，但 manifest 仍有 5 个区段、3 条 history。没有实际调用 Provider。
- **影响与方案**：排查模型遗漏信息时无法准确判断它实际看到了什么。先修复裁剪时同步更新实际请求清单，明确区分选择阶段清单与实际请求清单；后续由一个 ContextBudgetPlanner 统一产出最终消息、included/omitted/externalized 清单和摘要来源。
- **验收**：最终 manifest 对应实际请求；每次裁剪能追踪来源、原因和 checksum；不可压缩状态保留。大型结果优先扩展现有 Artifact/结果投影能力，避免另建重复事实源。

### F07 · P1：知识检索的外部等待仍处于外层 Session 锁中

- **证据**：[registry_reconcile.py](../backend/app/services/registry_reconcile.py) 第 382 行注册 inline `knowledge_search`；[ActionDispatcher](../backend/app/runtime/action_dispatcher.py) 第 765 行加 Session 行锁，第 1079–1081 行等待 executor；[knowledge_service.py](../backend/app/services/knowledge_service.py) 第 1283 行附近等待 embedding/Qdrant。
- **影响**：检索内部另开 detached session 不会释放 Dispatcher 外层事务。慢检索可能阻塞同 Session 的取消、事件写入和其他成员结算。调用链已确认，阻塞时长未压测。
- **方案**：优先复用已有持久异步 Tool 路径；或明确分为短事务登记、事务外 I/O、重新核验权限与执行所有权后的短事务结算。拆事务时保留幂等键、引用快照和当前权限复核。
- **验收**：用延迟检索注入配合真实 MySQL 验证，网络等待期间不持有 Session 写锁；取消命令无需等待检索超时；恢复后无重复调用结算。

### F08 · P2：校验错误响应回显密钥，并可能再次抛错

- **证据**：[main.py](../backend/app/main.py) 第 220–224 行将 Pydantic `errors()` 原样放入 JSONResponse；[ModelCreate schema](../backend/app/schemas/registry_v1.py) 第 119–123 行的 api_key 标记为 writeOnly。
- **验证**：合成超长 api_key 在 `issues[0].input` 中被完整回显；endpoint 自定义校验产生的 `ctx.error=ValueError` 无法序列化，原本 422 的错误处理再次失败。
- **方案**：白名单投影 `loc/type/msg`，移除原始 input/ctx，并审查自定义 msg 不包含敏感输入。统一前端错误读取和请求 ID 展示。
- **验收**：密码、密钥和断言等字段均不会通过错误响应反射；非法 endpoint 稳定返回可解析的 422。

### F09 · P1：恢复演练与当前版本脱节，备份缺少共同一致性点

- **证据**：[restore-drill.sh](../deploy/uat_single_node/restore-drill.sh) 第 99 行及 [deployment-drill.sh](../deploy/uat_single_node/deployment-drill.sh) 第 140 行硬编码 `0029`，当前 head 为 `0049`。[backup.sh](../deploy/uat_single_node/backup.sh) 第 26–42 行依次 dump 数据库、tar 文件、再读取实时卷算 checksum。
- **触发与影响**：最新数据库恢复无法通过旧版本断言；备份期间有写入时，DB、文件归档和文件清单可能不一致。恢复脚本又将旧备份行数与当前在线库比较，持续写入会导致错误判定。
- **方案**：备份 manifest 记录 schema、release、业务计数和引用边界；采用短暂停写/排空或可证明一致的快照；checksum 从冻结快照或归档计算。恢复比对备份 manifest，明确 Qdrant 的备份或重建路径以及密钥恢复方法。
- **验收**：在隔离环境恢复当前版本，完成登录、历史/Artifact、知识检索和未完成任务的业务冒烟；记录真实 RPO/RTO。回滚按版本兼容矩阵判断，不能简单把硬编码从 0029 改为 0049。

### F10 · P2：测试资产存在，但 CI 未覆盖关键边界

- **证据**：[GitHub CI](../.github/workflows/ci.yml) 只有仓库安全、后端单测和前端构建；[标准门禁](../test/scripts/run_ci_gates.sh) 已包含 MySQL 迁移及集成测试，但未接入该工作流。前端没有 test script。
- **额外漂移**：[UI 验收](../test/e2e/fullstack/test_ui_acceptance.py) 第 95–106 行仍检查桌面可见 MyClaw heading 与 `Agents`；当前登录 heading 为 `lg:hidden`、列表标题为“智能体”。这是静态定位失配，本次未声称实际跑过并失败。
- **方案**：PR 接入临时 MySQL 的迁移/约束/隔离测试和前端状态/交互测试；现有脚本对环境与清理有前提，应封装适合 CI 的隔离入口。真实模型验收仍独立运行；固定 API/SSE 数据仅用于测试客户端状态，不作为 Agent 能力通过证据。
- **验收**：分页、账号切换、SSE 重复/断线/游标过期、审批版本冲突和 F01 迟到行动均进入门禁；补上一个支持版本到当前版本的升级验证。

## 4. 第二阶段优化

### Runtime 与上下文

先固定状态迁移合同，再按职责抽取 `RunnerOwnership`、`ContextBudgetPlanner`、`ActionLifecycle` 和模式结算服务。这些是建议职责名，不要求引入同名新框架。Worker 仍负责领取工作、启动/停止任务和驱动这些服务；Chat/Plan/Group 继续共享循环。

`runtime_worker.py` 约 3,279 行，Dispatcher 约 1,133 行。重构理由是执行有效性、事务边界与结算分支散布在多处；拆文件本身不能解决问题。每个 PR 只抽取一个职责，以取消、恢复、重复投递、晚到结果和权限失败回归保持行为。

在既有 ExecutionEvent 上增加版本化 UI 投影，稳定携带 sequence、entity、causation、visibility 与 committed/provisional 区别。REST 接收命令、SSE 分发投影，领域实体继续决定业务状态。

### 前端实时数据与可用性

[Session 页面](../frontend/src/pages/Session/index.tsx) 第 261–287 行会按事件使多组查询失效，同时保留多路 1–5 秒轮询；[SessionInspector](../frontend/src/pages/Session/SessionInspector.tsx) 第 50–62 行从 cursor=0 重拉且最多 20 页，随后把该前缀末尾显示为“最近”。

抽取 `useSessionRuntime`：维护增量游标、按实体合并更新、100–250ms 合并刷新；健康 SSE 下减小轮询，断连时有界补偿。时间线初始化支持最新窗口，明确 `has_more`；2,001+ 事件仍须展示真正终态。先测 requests/min、传输字节和 event-to-render p95，再确定优化幅度。

补齐加载/空/错误三态、重试入口和错误边界；“平台服务正常”应来自真实健康信号。移动端会话导航当前隐藏在 `lg` 以下，应根据目标用户决定补 Drawer/390px 冒烟还是明确桌面支持范围。

前端已有路由懒加载、Markdown 清理和流去重，无需把打包工具或 UI 组件库更换列为本轮主项。

### 检索与性能

先测量三类开销：模型调用前处理、检索延迟、数据库事务等待。

- [Runtime Worker](../backend/app/workers/runtime_worker.py) 第 1113 行及 [Coordinator](../backend/app/runtime/agent_loop_coordinator.py) 第 218 行逐条裁历史并重算 token；Anthropic count 是网络调用。改为有界批量裁剪/边界搜索、末次精确核验，并复用未改变请求的计数。
- [knowledge_service.py](../backend/app/services/knowledge_service.py) 第 1411 行附近在线读取候选来源完整文件并计算 checksum。先按最终结果渐进验证，再评估把完整性检查移到入库/后台并保存可验证状态；不能牺牲权限隔离或精确引用。
- 以 1/10/50 并发 Run、1/10/50 个授权知识库、长历史和慢 Provider 建立容量基线；指标至少含 p95/p99、排队时长、SQL 锁等待、连接池等待、读取字节和 token-count 调用次数。并发数是建议测试阶梯，不是当前容量承诺。

### 安全、发布与运维

[config.py](../backend/app/config.py) 第 223–231 行的 production 校验只排除少数占位值。合成验证发现空 SECRET_KEY、空 ENCRYPTION_KEY 和一字符管理员密码可通过；[platform_secrets.py](../backend/app/core/platform_secrets.py) 会对非标准长度字符串派生密钥。补格式、长度与缺失值校验，保持开发环境可用性；生产配置启动校验通过也不等于整体生产就绪。

[auth.py](../backend/app/api/auth.py) 的 logout 不撤销 token，refresh 可用有效 access token 换新 token。明确退出语义，设计可撤销 session、有限绝对寿命及 refresh 轮换；若先利用 auth_revision，要明确其退出所有设备的影响。

[main.py](../backend/app/main.py) 的 `/health` 仅证明进程活着。区分 liveness、readiness 和可降级依赖；readiness 有界检查数据库/schema，页面结合现有 Worker heartbeat 展示状态。

Python 直接依赖已有固定版本，前端已有 lockfile；后续补传递依赖锁定/哈希、镜像 digest 和周期性漏洞扫描。本次没有据此推断任何具体 CVE。

新迁移 `0049` 将调度定义转向 ToolInvocation 引用；其 downgrade 使用占位文本恢复若干字段。应在本轮基线收口时验证真实数据升级、保留策略及回滚后的调度行为，不能只验证 schema 能降级。先前未提交改动应独立整理，避免与通用重构混成一个 PR。

### 产品与质量经济

沿用 [产品价值验证](modules/product-validation.md) 的指标，不以功能数量或 Agent 数量作为成效。建议选一个有真实数据 Owner、重复发生的任务，建立单 Agent、按需委托、Group 三组对照；按业务接受率、人工干预、完成时间、返工和每个被接受结果的成本评估。

现有 [Agent Eval](../backend/app/services/agent_eval_service.py) 第 42–88 行主要验证文本包含/排除、长度和成本，适合作为基础合同。增加与 Tool 结果、Artifact checksum、引用和业务状态绑定的断言，统计多个样本的波动，再做候选版本与已发布版本对照。

现有 Run 预算已限制 token、调用数和时间；[成本账本](../backend/app/services/usage_ledger_service.py)也区分未知价格。后续把货币预算预警/阻断接到同一账本，在并发行动前做额度预留并在结算后核销，避免只事后统计或将未知价格视为零。

## 5. 可执行的交付顺序

以下是逻辑 PR 切分与粗略工作量级，S≈1–2、M≈3–5、L≈5–8 工程人日，包含针对性测试；不包含等待外部 UAT 环境、评审或设计伙伴反馈。它们是规划估计，不是排期承诺。

| 顺序 / PR | 交付范围 | 依赖 | 量级 | 合入条件 |
| --- | --- | --- | --- | --- |
| 00 基线收口 | 整理已有 Schedule/0049 与参数处理变更，固定可复现 commit；校正文档版本 | 无 | S–M | 现有单测/构建通过，真实 MySQL 升级验证，明确回滚支持范围 |
| 01 执行所有权 | 修 F01，统一行动提交前 fence；保留迟到事实 | 00 | M | cancelled/旧 epoch/替换 Runner 三类并发回归，零新增行动 |
| 02 账号与消息 | 修 F02/F03；query namespace、完整分页、最新消息窗口 | 00，可与 01 并行 | M | 双账号切换与 101 消息/51 会话测试通过 |
| 03 API 安全边界 | 修 F04/F08，生产配置校验 | 00，可并行 | S–M | 假头限流、密钥不反射、422 稳定、非法 production 配置拒绝 |
| 04 持续门禁 | 接 MySQL 与前端交互测试，修现有 UI 定位 | 00 起步，随 01–03 扩展 | M | 普通 PR 可自动检出本轮边界回归，无真实模型费用 |
| 05a 上下文正确性 | 分别修 F05 滚动摘要、F06 实际请求清单 | 00、04；可与 01 并行 | M，两个小 PR | 多次摘要覆盖推进，manifest 一致，关键状态保留 |
| 05b 预算与结果投影 | 统一预算职责、大结果投影及计数次数优化 | 01、05a | M–L，分批交付 | 请求等价与边界测试通过；以基线证明计数调用/前处理开销下降 |
| 06 检索事务 | 修 F07，登记/I/O/结算分离，建立检索容量基线 | 01、04 | M–L | 慢检索不占 Session 写锁，取消与恢复不重复结算 |
| 07 恢复可信度 | 修 F09，版本化 manifest、一致备份、完整恢复冒烟、readiness | 00、04 | M–L | 当前 release 的隔离恢复成功，业务不变量和 RPO/RTO 留证 |
| 08 Runtime/实时层整理 | 分职责抽取，事件版本投影，增量时间线，削减重复回源 | 05a、06；前端依赖 02、04 | L，拆成小 PR | 状态机回归通过；以基线证明请求量/锁等待改善 |
| 09 质量与采用验证 | 真实任务对照、证据型 Eval、成本预警；完善 session 撤销 | 关键 P1 修复后 | 分批立项 | 预先登记接受标准并报告失败样本，确认第二个流程的复用价值 |

执行建议：第一轮交付 00–04，并尽早合入 05a，使关键问题能被持续检出；第二轮交付 06–07，使检索与恢复可信；第三轮分批推进 05b、08，并让 09 的用户任务验证反过来决定后续功能投入。

## 6. 完成标准与暂缓项

每个修复同时提交行为说明、针对性回归和必要的 API/状态机文档变化。涉及迁移的 PR 以真实 MySQL 验证；涉及模型行为的变更在受控真实 Provider 上验收。发布前将全部结果绑定干净 commit，保存脱敏摘要及受访问控制的原始证据。

本轮暂缓微服务/Kubernetes 扩张、第二套 Runtime 状态账本、通用双向宿主协议、插件市场和为所有场景默认开启 Group。启动这些工作的条件分别应是容量瓶颈证据、现有投影无法满足的明确需求、真实外部客户端或可重复的业务价值，而非架构形式上的完整性。

本方案最先要实现的用户结果是：**退出后数据不串号，长对话不丢最新回答，取消后不再产生新行动，长任务不会因摘要停滞失去中段上下文，备份确实能够恢复到可用状态。**
