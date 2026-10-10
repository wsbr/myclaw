# Agent 装配

本文是 Agent 身份、专业配置、版本、测试、发布和 AgentCard 的唯一权威来源。

## Agent 是逻辑主体，配置只是其发布蓝图

一个可用 Agent 必须能够独立理解领域目标、使用环境、处理失败并交付结果。`Agent` 保存稳定身份、Owner 和治理边界；平台真正编辑和发布的是它的配置蓝图 `AgentVersion`：

```text
AgentDefinition
├── identity and purpose
├── instructions
├── model
├── tools and skills
├── context and memory policies
├── sandbox / environment
├── discovery and collaboration settings
└── governance metadata
```

平台不根据“产品经理”“开发”“测试”等名称自动补 Tool、Prompt、权限或工作流。

Agent 平时不对应常驻进程。收到请求后，Runtime 才把指定 AgentVersion 与当前 Session、WorkItem、CapabilitySnapshot 和环境装配成 Runner。相同 AgentVersion 可以并发运行多个 Runner；它们默认是同一 Agent 的执行实例，只有不同 `agent_id` 才表示不同逻辑主体。

## Agent 与 AgentVersion

`Agent` 是稳定身份，`AgentVersion` 是不可变发布快照：

- Draft 可以编辑和测试；
- Publish 解析全部依赖、生成不可变版本，并以 CAS 更新 Agent 的当前发布版本指针；
- Run 保存实际 AgentVersion；
- WorkItem 保存跨等待工作，Runner 只保存某段连续执行；
- 新版本不改变已开始的 Run、Invocation 和历史；
- 停用阻止新的选择，不删除历史事实。

版本至少冻结 Model、Instructions、Tool、Skill、四类 Context/Memory Policy、Sandbox、发现配置及各依赖版本/checksum。Draft 选择逻辑 `model_id/policy_id/sandbox_profile_id/skill_id`，Publish 与 draft_test_snapshot 分别解析并固定 `model_version_id`、四个 `policy_version_id`、`sandbox_profile_version_id`、`skill_version_id + checksum`；Runner 不追随任何 current pointer。Tool 的 `registration_hash` 固定在 AgentVersion，但启停、Confirmation、风险和连接配置是明确的实时治理例外，执行前重读。

Runtime 协调工具与文件、Web、MCP 工具使用同一个 Tool 绑定列表。`ask_user`、`update_plan`、`invoke_agent` 等不再自动附加；需要时在草稿中显式选择并发布。注册升级不修改历史发布版本，不自动修改草稿选择。使用 Plan 模式需要有效的 `update_plan` 绑定，Group 协作需要按实际职责绑定相应协调工具。

工作网默认只投影 Agent 当前有效的发布版本。普通 Chat/Plan 委派在创建 AgentInvocation 时解析并固定目标版本；GroupRun 优先使用自己的成员版本快照。停用 Agent 或撤下当前版本会阻止新的解析，但不改变已经创建的 WorkItem、Invocation 和历史。

## 身份与专业边界

Agent 说明：

- 代表个人、岗位、业务域还是公共能力；
- 服务对象和专业目标；
- 擅长完成的结果；
- 不负责或必须升级的边界；
- Owner 和治理范围。

身份描述影响模型上下文和网络发现，不直接产生数据或 Tool 权限。

## Instructions 是主要行为载体

Instructions 用自然语言表达：

- 专业知识和判断原则；
- 工作方法、验证习惯和失败处理；
- 沟通风格、耐心、严谨程度和协作偏好；
- 需要的输入、交付格式和风险边界；
- 何时澄清、何时找人和何时拒绝。

平台可以提供可选标签帮助 Builder 编辑和用户发现，例如 `thorough`、`fast`、`adversarial_review`，但标签不应成为 Runtime 的强制行为状态机。真实人格由 Model、Instructions、Memory、Skill 和经历共同形成。

## Model、Tool、Skill、Memory 与 Environment

- **Model** 提供认知和推理能力；
- **Tool** 提供真实行动接口；
- **Skill** 提供可复用方法、参考和脚本；
- **Context/Memory** 提供当前信息与可持久经验；
- **Sandbox/Environment** 决定 Agent 实际能观察和改变什么。

这些能力相互正交：Skill 不自动获得 Tool，Agent 名称不自动获得权限，Memory 不等于知识库授权，发现其他 Agent 不等于允许调用。

## AgentCard

发布版本生成面向工作网的 AgentCard：

```text
agent_id / agent_version_id
name / description / identity_kind
domain_tags
supported_outcomes
accepted_request_summary
capabilities_summary
collaboration_summary
cost / latency / availability
trust / owner / governance labels
```

AgentCard 是安全发现投影，不包含 Instructions、私有 Memory、Prompt、凭证和 Chain-of-Thought。AgentCard 变化随新版本发布，不原地修改历史卡片。

Card 中必须区分 Builder 声明与平台证明：description、domain tags、supported outcomes 属于声明；Owner、发布状态、装配摘要、Eval/运行证据和治理标签由平台派生或签发。推荐可以综合两者，但不能把自述能力显示成已经验证的信誉。

## 工作网发布

当前可持久化的 Agent visibility 只有：

```text
private | enterprise
```

`private` 默认只允许 Owner 使用；Owner 可以把当前 tenant 的有效普通成员加入 Agent 白名单，使其获得“发现 + 对话/调用”权限。该授权不授予草稿配置、测试、发布、评测、API Key 或白名单管理能力，Owner 也不会被复制成一条成员记录。`enterprise` 表示当前 tenant 的有效成员都可发现和使用。两者仍要经过发布状态、Collaboration Policy、原始账号和环境兼容校验。未来若引入协作者、team 或 public-federated 等细粒度 Scope，必须新增明确的权限模型和迁移，不能把这些概念词直接写入当前枚举。

成员授权是实时治理事实，不冻结进 AgentVersion 或 AgentCard。创建 Session、发送后续消息、Agent Network 发现/调用、Group 成员选择和定时任务分派都调用同一授权边界。撤销只阻止新的运行和分派，不删除成员自己的历史 Session，也不改写已经开始的 Runner 快照。

还可以配置显式调用关系、组织标签、可用时间和成本提示。调用关系限定可委派对象；是否调用及如何协作仍由当前 Agent 根据任务判断，不创建必执行流程或 Group 模板。

`bound_agent_ids[]` 是逐跳的显式调用关系，不要求双方开启 `group_enabled`。例如 A 绑定 B、B 绑定 C 时，允许 A→B→C，每一跳都按当前调用方的发布版本重新校验；这不等于 A 获得直接调用 C 的权限。结果逐级返回，各跳始终沿用原始用户身份和权限。

`group_enabled` 是双向的协作网络参与资格：调用方与目标方都开启时，在原始用户可见、可调用及平台治理允许的范围内，无需互相绑定即可发现、委派或被选入 Group。普通用户可以直接 Chat A，由 A 判断是否需要协作、寻找成员，并自动形成包含 A 的群组继续工作。每个 GroupRun 固定成员与版本，成员调整在后续新 Run 生效。

两条路径都不自动授予工具。发现、委派和组队所需的 `discover_agents`、`invoke_agent`、`select_group_members` 等仍须分别在相应 AgentVersion 显式绑定并通过工具治理。网络开关和 Agent 绑定都不扩大原始用户的数据权限，也不保证当前环境可以成功运行。

## 权限忠实性

发布版本记录装配能力；每次 Runner 再与原始用户、permission mode、Group Membership、Tool Governance 和预算求交集。

Runtime 必须忠实于结果：

- 不因 Instructions 看起来需要而补 Tool；
- 不因 Agent 身份看起来可信而扩大数据；
- 不因 Provider 不兼容而静默换模型；
- 不因某 Tool 使用过一次而从版本中删除；
- 不让目标 Agent 的 Credential 为调用者提权。

## Draft Test

发布前测试至少覆盖：

- Model 连通、streaming、Tool Calling 和 stop reason；
- Tool schema、权限和 Sandbox 可用性；
- Skill 路径、checksum 和必要资源；
- Context/Memory Policy 解析；
- 自然语言领域样例；
- 缺少权限、Tool 失败和需要澄清的行为；
- AgentCard 内容与私密信息泄漏检查；
- 作为单 Agent 和被调用 Agent 的最终消息路由。

可加入 Group 的 Agent 还应验证 direct/group Message、共享上下文边界和 AgentInvocation。

Draft Test 只能证明当前装配候选，不自动发布，也不代表业务 UAT 已通过。

### 版本化 Eval 与发布门禁

工作区侧栏「评测」(`/evals`) 提供独立评测工作台，Agent 配置页保留跳转入口。Builder 管理自己拥有的 Agent，管理员可管理当前 tenant 的 Agent。界面支持：

- 手工创建用例、从自己的真实 Chat 运行预览导入、场景标签、权重和表单化断言编辑；
- 评测集 JSON 导入/导出、启停、不可变版本保存；移除用例只影响新版本；
- 当前已保存草稿的真实批量评测、进度、逐项断言、实际回答、运行详情与报告导出；
- 同一评测集版本的两次已结束运行对比，展示加权通过率差值、退步/改善用例、回答对照及标签切片通过数。

套件名称、门禁策略、输入、权重、断言、标签及来源快照按 SuiteVersion 冻结，纳入内容哈希。编辑通过 `expected_version` 检查并发更新，保存新版本，不覆盖旧定义。启动时平台生成与发布内容哈希一致的 `draft_test_snapshot`，再为每个用例建立独立真实 Session/Run；不另建评测模型调用链。使用真实模型和已绑定工具，记录原有 ProviderAttempt、ToolInvocation 和 Usage 账本。需要用户答复或审批时，从逐用例的「查看过程」进入原运行处理，不能假定已经通过。

历史导入通过 `tenant_id + account_id + user_id + root Agent` 校验，只接受已结束的 Chat Run。拥有 Agent 或管理员角色不会额外获得其他用户对话的导入权限。只提取本轮 root WorkItem 输入、面向用户的最终回答、工具名称/状态和配置版本指纹，不导入系统提示词、凭据、历史消息或工具原始参数。来源回答和观察到的工具行为仅供参考，不自动当成金标，也不注入被测模型。依赖多轮历史、文件或记忆的样例需先整理为自包含输入。跨环境 JSON 导出只携带可移植的用例定义，不携带来源会话标识和原回答。

维护 Worker 在 Run 终态后，对被测 Agent 面向用户的 Chat 最终 Message 结算；不会误取后续子 Agent 消息。每个用例全部断言通过才得分，套件得分按权重计算：

| 断言 | 判定依据 |
| --- | --- |
| `contains_all` / `contains_none` | 最终回答包含/不包含指定文本，区分大小写 |
| `min_chars` / `max_chars` | 最终回答字符数 |
| `require_tools` | 本 Run 内存在该工具的持久化 ToolInvocation，含失败/拒绝记录 |
| `require_successful_tools` | 本 Run 内至少一次该工具调用状态为 succeeded |
| `forbid_tools` | 本 Run 内不存在该工具的持久化 ToolInvocation |
| `max_cost` | Usage 账本中指定币种的成本；未知或不可换算成本默认不能冒充 0 成本 |

工具断言覆盖同 Run 的持久化调用，不能证明模型没有在文字或未落库的拒绝尝试中提及某个工具。运行失败、取消或缺少最终交付记为 error，不能靠降低通过率门槛放行。当前执行范围为单 Chat 草稿测试，不伪装成 Group/Plan 全流程回放。

发布只要求当前 active 且 `gate_policy.required_for_publish != false` 的 SuiteVersion，在当前精确 candidate content hash 下存在 passed 终态 EvalRun。新工作台创建套件默认是日常回归，可显式开启发布门禁；旧套件缺省仍作为门禁。停用套件不会删除历史，但不再参与发布也不能新建评测运行。草稿或 Model/Tool/Skill/Policy/Sandbox 依赖变化、门禁定义版本变化都会使旧成绩失效。

对比仅允许同 Agent、同不可变 SuiteVersion 且用例完整的终态运行。相同候选指纹会提示这是运行波动，不能声称配置提升。历史结果重判、轨迹相似度、LLM 裁判、线上健康分和 Group 多轮评测不在当前实现范围。

## Agent 的专业成长

运行反馈可以形成改进候选：

- Instructions 调整；
- 新 Skill 或参考资产；
- Tool 文档和接口改进；
- Memory 规则调整；
- 更合适的 Model；
- AgentCard 描述和发现标签；
- 领域 Eval 用例。

企业 Agent 的正式变化由 Builder 或 Owner 审核后发布新版本。个人 Agent 可以在个人数据边界内持续学习偏好，但必须可见、可撤销和可清除。

## 第一期 Agent

第一阶段使用五个逻辑 Agent：个人工作助手、项目经理、产品、开发和测试。它们是 UAT Fixture，不是平台内置角色。

专业差异必须来自真实 Instructions、Tool、Skill 和环境：

- 个人助手擅长澄清和整理；
- 项目经理擅长计划、协调和风险；
- 产品擅长需求口径和验收；
- 开发擅长实现、执行和修复；
- 测试擅长精确用例、反证和复核。

入口与产品、开发、测试 Agent 发布到同一工作网范围并开启网络参与资格，供普通 Chat 中的 Agent 按任务需要选择成员。所需工具分别显式绑定；Group 不复制其定义，也不通过模板补齐能力。

## 禁止设计

- 仅靠名称和短 Prompt 冒充专业 Agent；
- 用强制 Profile 枚举替代 Instructions、Memory 和 Skill；
- 根据角色自动授予 Tool 或数据；
- 把逐跳显式绑定编译成必须执行的流程，或传递成入口 Agent 的直接授权；
- 为 Group 单独复制 Agent 配置；
- 发布时接受未解析依赖或隐式 fallback；
- 让新版本改变历史 Run；
- 在 AgentCard 暴露私有 Prompt、Memory、Credential 或 Chain-of-Thought。
