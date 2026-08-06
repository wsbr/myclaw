# V1 前端设计（历史实现参考）

> **历史 V1 实现参考，非目标设计。** 后台执行、Required Action 和断线恢复仅作为迁移证据；当前 WorkingPlan、Plan 只读批准、工作网选人、Group 消息/实际协作图和 Verification 体验以[综合技术方案](phase-one-agent-native-delivery.md)为准。

本文记录V1页面、客户端状态、交互与现有页面处置基线。React/Vite、Router、TanStack Query、Zustand、Ant Design与React Flow继续使用。

## 1. 客户端分层

- Server state全部用TanStack Query，query key以`user_id/resource/id/version`稳定组织；命令成功以响应对象更新缓存，再按需invalidate。
- Auth/UI偏好用Zustand；Run、图、Confirmation不得只存在Zustand。
- `EventStreamManager`按Session维护单连接、last_sequence、event_id LRU去重、指数退避与补读；重连先REST补洞再开SSE。
- `RequiredActionStore`由ExecutionEvent驱动并以REST列表校准，刷新页面不丢。
- 模型delta只更新临时气泡；`message.created`替换为持久Message。断线时显示“后台继续”，不把临时文本当最终结果。

## 2. Agent列表与发布

列表卡显示name/description、draft状态、最新published版本、active/inactive、visibility、Group标记。Builder看到“编辑草稿/发布/停用”，用户只看到可用Agent与“开始会话”。草稿未发布不能进入普通对话。

发布流程：保存draft（If-Match）→显示校验问题→发布确认→展示新published version_no/content_hash。发布后当前draft ID不变、draft_revision递增并显示“已基于 vN，可继续编辑”；前端不得把draft替换为published对象或清空表单。停用明确提示只阻止新执行；inactive Agent 发布新版本后仍 inactive。版本列表只读展示装配差异与历史引用。

“测试草稿”按钮携带expected_draft_revision，服务端复制immutable draft_test_snapshot后创建带明显测试标识的私有Chat Run；页面说明后续编辑不改变本测试，且该Run不能进入Plan/Group或被其他Agent调用。

## 3. Agent配置五区域

使用一个草稿表单和固定五区导航：

1. 基础信息：name、description、visibility、Agent状态只读/动作。
2. 模型与指令：单选Model、单选Sandbox、唯一instructions编辑器；不显示temperature/top_p/max_tokens。
3. 能力装配：同一Platform Tool选择器与独立Skill选择器；无“builtin/custom”双列表，无MCP。
4. 上下文与记忆：四个Policy单选，只展示说明，不展示内部阈值/TTL。
5. 协作与访问：bound Agents、group_enabled；不编辑Capability/Event Manifest。

离开脏草稿前提示；并发保存409显示服务端版本和“重新载入/复制本地内容”，不自动覆盖。

## 4. 管理页

Tool列表通过分页集合接口展示key、显示名、execution_mode、enabled、confirmation_policy、implementation_version、使用Agent数；行内可启停并在 `never|per_call|session_grant` 间切换，详情编辑说明/平台配置、看Schema/连通测试。`per_call` 每次创建 Required Action，`session_grant` 仍需用户先授予受限 Grant；危险变更显示即时语义。

Skill页支持拖拽包上传、解析进度、校验错误、版本历史、publish、共享同步状态与checksum；不提供“提示词内容”编辑器。Model页支持创建、编辑、启停和连通测试；Policy/Sandbox页提供分页列表、详情与允许字段编辑。Agent表单只调用 `/agent-options/*` 安全选择集合，不能复用admin详情。管理侧新增Platform Audit分页页，按resource/actor/action/outcome过滤；MCP页面和导航第一阶段移除/后置。

## 5. Session统一工作区

Session页面由左侧历史、顶部模式/后台状态、中央作用域、右侧可折叠Timeline/Artifact组成。输入前明确当前scope：`Chat / Plan Owner / Group: Agent Name`。

- Chat：消息、流式临时气泡、Tool/Agent调用状态卡、Clarification/Confirmation内联卡。
- Plan：与同Session Chat共享外壳但切换到Plan子空间；终态结果回Chat只显示摘要引用。
- Group：作为Session子空间；返回Chat只改路由/选中scope，后台继续。

Run 卡展示权威 status、等待原因和 terminal result；页面关闭不发送 cancel。取消必须二次确认并说明不回滚副作用。

## 6. Plan图

Proposal使用React Flow节点=WorkItem，侧栏编辑goal、执行Agent、验收条件、依赖。每次保存服务端校验DAG并返回graph_revision；确认页展示参与AgentVersion、冻结提示和差异。确认后图只读，运行状态叠加而不改定义。

并行节点、waiting/failed/blocked/partial有独立视觉；节点详情展示WorkItem、Runner attempts、Invocation、Artifact。计划失效提供“基于结果创建后继Plan”，不编辑运行图。

## 7. Group页面

Formation页由 `GroupFormationProposal + GroupSession(forming) + current immutable revision` 驱动，同时编辑goal、Bootstrap Context及来源、参与Agent、初始职责、初始WorkItem和建议边，明确“不会共享完整Chat历史”。每次保存发送expected_revision_no+If-Match；409展示服务端新revision和本地差异，不覆盖。拒绝/取消有独立动作。确认后才进入 open GroupSession 执行视图：节点=GroupAgentNode；建议边保持虚线，只在真实invoke_agent后出现实际实线边；参与集合只读。

群记录按sequence展示，支持按Agent/WorkItem/调用链过滤。输入框必须选择recipient，空值明确标为入口Agent。Clarification卡标发起Agent。用户可：返回Chat、取消当前GroupRun、关闭Group，三个动作不得复用同一按钮。

终态GroupRun后的输入创建新Run并显示引用；直接@可用节点使其成为owner。停用节点保留在固定图并显示unavailable、原因和禁用的定向动作，不隐藏、不替换；非owner停用不阻塞其他节点继续。若默认入口节点不可用，新Run composer必须要求用户从可用固定节点中选择owner；没有可用节点时引导新建Group。向已冻结为unavailable的节点发送动作遇到409时保留输入并刷新节点状态。Agent重新启用只在下一GroupRun生效，前端不得把当前轮按钮即时解禁。父Chat仅展示Group状态卡和结果摘要链接。

## 8. Clarification与Tool Confirmation

Required Action卡可在消息流、全局待处理抽屉和Session顶部出现。Clarification显示稳定问题、发起Agent、scope；提交后锁定答案。Tool Confirmation必须显示Tool、精确参数的安全渲染、Agent、影响说明及批准/拒绝；参数hash改变展示为新卡。外部API处理后SSE即时收起。

## 9. Timeline、Trace与Artifact

Timeline读取统一 `session_user` 可见性投影，只展示返回items；sequence空洞不渲染“丢失”，cursor frame只更新last_sequence。默认显示用户安全摘要，可展开允许的实体版本、因果链、Runner/Invocation；direct/private记录不会因admin或builder登录自动出现。Trace是开发/管理员脱敏视图，按独立Runner Trace和Span Link展示，不提供Fork按钮。Artifact面板显示版本链、来源Agent/WorkItem/checksum、预览/下载与unavailable状态。

## 10. 重连与后台状态

SSE/Redis bridge断开：顶部黄条“实时输出中断，任务仍在后台执行”；丢弃不完整delta气泡的权威性标识，继续等待最终Message。SSE按1/2/5/10/30秒重连，用last scanned sequence REST补读；即使补读items为空也采用scanned_through_sequence。401先刷新认证；410全量刷新Session snapshot与最新cursor。浏览器刷新从Session、Messages、Runs、Required Actions、last events重建，不从本地猜状态。

## 11. Memory 与通知入口

个人设置新增“Memory”入口：按 `user/user_agent` 与 Agent筛选，分页显示fact_key、内容、来源、更新时间；详情展示可访问来源和版本历史。编辑要求最新head_version，冲突时重载对比；删除二次确认并提示“后续Runner立即停止召回，历史Runner快照不变”。Builder/Admin切换管理身份也不能查看其他用户Memory。

Session顶部来源状态展示 Web/API/Channel。Web只显示Required Action和事件，不显示虚假“已通知”；API显示“由调用方轮询/SSE”；Channel Session可展开NotificationDelivery安全状态、重试次数和失败码。Notification失败不把已完成Run卡改成失败。全局NotificationBell只聚合当前用户Required Actions，不冒充Channel投递记录。

## 12. 现有页面处置

| 现有实现 | 处置 | 说明 |
| --- | --- | --- |
| App/WorkspaceLayout/Login/基础UI | 小幅改造 | 保留路由壳、鉴权和设计系统；按角色隐藏管理导航 |
| Agents/List | 大幅改造 | 卡片壳可留，状态/权限/发布动作改写 |
| Agents/Config | 替换 | 旧多Prompt、模型参数、双Tool、MCP、Manifest、告警均不符合五区契约 |
| Session | 替换 | 旧同步chat流+本地Plan状态改为持久Run/SSE/三模式scope |
| Orchestration | 替换 | 旧Group Task DAG改为Formation/GroupSession/Agent节点 |
| Trace | 大幅改造 | 保留树/详情组件，移除Fork，增加Timeline与跨Runner Link |
| Tasks | 删除或后置 | 产品Task看板被Run/WorkItem后台执行视图替代 |
| ToolsTab | 大幅改造 | 增加治理字段与操作，不创建实现 |
| SkillsTab | 替换 | 改包上传、版本、checksum、同步 |
| ModelsTab/UsersTab | 大幅改造 | 复用表格/弹窗，适配Model实体、userId、三角色 |
| McpTab | 删除或后置 | 第一阶段无MCP |
| NotificationBell | 大幅改造 | 改为Required Actions + 来源通知状态，不能只轮询旧通知 |

## 13. 前端验收测试

组件测试覆盖五区表单、发布后draft rebase、错误/冲突、状态卡、Memory编辑删除、Notification来源差异；契约测试用生成OpenAPI类型；Playwright覆盖发布→Chat Redis降级/断线恢复、Confirmation、Plan编辑冻结、Group Formation revision/建议边与实际边/定向消息/返回/取消/关闭、Artifact下载、事件visibility过滤和cursor空洞。禁止只测试页面可打开。
