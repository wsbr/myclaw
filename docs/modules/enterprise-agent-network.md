# Enterprise Agent Network

本文是 MyClaw 企业 Agent 工作网和未来跨域联邦的唯一产品权威。它先定义同一 MyClaw 控制域内可以落地的逻辑网络，再说明跨实例、跨组织的演进边界；公网协议不是第一阶段前提，也不能反向增加平台内部复杂度。

## 产品北极星

MyClaw 的终局是由完整 Agent 组成的动态企业工作网：

- 每个人可以拥有代表其偏好、习惯和受托边界的个人 Agent；
- 每个岗位、专业和业务域可以拥有积累经验的垂类 Agent；
- 企业公共能力可以作为公共 Agent 被发现和协作；
- Agent 可以更新版本、沉淀 Skill、形成信誉并受控下线；
- 人给出目标后，Agent 自主理解、规划、行动、验证和寻找协作者；
- Agent 缺少信息、授权或价值判断时，才定向找人。

工作网不是中央超级 Agent，也不是预先配置角色和流程的模板市场。网络价值来自每个节点本身的专业性，以及节点根据真实目标形成合作的能力。

## 网络首先是控制平面

同一 MyClaw 平台中的 Agent 主要是数据库中的逻辑身份和版本化配置，不是各自暴露 IP 的常驻服务。工作网分成：

```text
Control plane
Agent / AgentVersion / AgentCard / visibility / Membership / policy

Execution plane
WorkItem / Runner / AgentInvocation / Message / Artifact / Event
```

控制平面回答“有哪些 Agent、当前能否发现和建立关系”；执行平面按需加载配置、运行 Agent Loop 并传递结果。Agent 空闲时可以没有 Runner，网络节点仍然存在。

第一阶段的目录是 MySQL 中的 AgentCard 投影和受治理查询。未来跨 MyClaw 控制域时才增加 Agent Gateway、远程 Endpoint、协议协商和联邦身份；本地调用与远程调用在产品层都保持 AgentInvocation 语义。

MyClaw Runtime 为了装配本地 Agent 可以读取其 AgentVersion，但协作边界仍把 Agent 视为黑盒：其他 Agent 只能依赖 AgentCard、Message、Task/Invocation、Artifact 和可观察结果，不能读取对方 Prompt、Memory、内部 Tool 或推理。远程 Agent 只需满足同一外部契约，不要求采用 MyClaw Harness。

## Agent 是网络节点

Agent 是完整的逻辑工作主体，可以单独面对用户，也可以临时参与多 Agent 协作。它不等于 AgentVersion 配置，也不等于运行该配置的 Runner。它可以代表：

- `personal`：具体个人；
- `role`：岗位或职责；
- `domain`：业务域或专业能力；
- `public`：企业公共服务。

身份分类只影响描述、治理和发现，不产生第二套 Runtime。所有 Agent 共用同一个 Agent Loop、Tool、等待、恢复、计划和消息协议。

Agent 的专业差异来自 Model、Instructions、Tool、Skill、知识、Memory 和历史经验。严谨程度、耐心和协作习惯可以自然写入这些装配内容；平台不需要把人格拆成强制枚举才能形成差异。

## Agent 先专业，再入网

进入工作网的 Agent 至少需要：

1. 清楚说明代表谁、服务谁、擅长什么和不负责什么；
2. 拥有能完成领域工作的 Model、Instructions、Tool、Skill、知识和环境；
3. 知道何时验证、何时继续尝试、何时澄清和何时升级；
4. 能向人和其他 Agent 说明需要的上下文和交付结果；
5. 对敏感数据、记忆和共享范围有明确边界；
6. 在领域 UAT 中证明其单体工作质量。

Group 不能用多个空洞角色 Prompt 掩盖单 Agent 能力缺失。

## AgentCard 与发现

AgentVersion 发布后生成面向工作网的 AgentCard：

```text
agent_id / agent_version_id
name / description / identity_kind
domain_tags / capabilities
supported_outcomes
accepted_request_summary
tools_and_environment_summary
cost / latency / availability
trust / governance labels
owner / contact boundary
```

AgentCard 不公开 Instructions、私有 Memory、完整 Prompt、凭证或 Chain-of-Thought。`group_enabled` 只是“可以成为 Group 候选”的声明，不能单独推出 visibility、可调用性、授权或运行可用性。

工作网必须区分：

- **精确寻址**：已知稳定 `agent_id`，解析当前允许使用的发布版本；
- **语义发现**：根据能力声明、治理标签、成本和历史质量返回候选。

工作网提供搜索、推荐、组织目录、历史合作和直接 ID 寻址。发现回答“谁可能适合”，不回答“当前是否有权调用”；邀请或调用仍需经过原始用户、Agent 装配、共享范围和平台治理的机器校验。AgentCard 中的能力是声明，平台评测和历史结果是独立证据，不能混成一个可信字段。

## 自然形成协作

```text
人向当前 Agent 提出目标
        ↓
Agent 自主工作并判断是否需要协作
        ↓
在工作网搜索或使用已知 Agent
        ↓
发出邀请 / 定向工作请求
        ↓
对方接受、追问、反提议、拒绝或工作
        ↓
形成 Subagent 委派或 Group Membership
```

平台不要求预先存在 Group 模板、角色槽位或固定调用图。重复业务场景可以保存为用户自己的场景提示、验收夹具或成员收藏，但它们不是 Group 本体。

## 两类多 Agent 关系

### 委派

Chat 或 Plan 中，当前 Agent 为一个有边界的问题调用另一个 Agent。目标 Agent 在独立上下文中工作，最终结果返回调用方，由调用方继续对用户负责。

适合探索、测试、评审、摘要等只需要结果的任务。

### Group

多个 Agent 需要直接通信、共享任务状态、相互质疑或接受用户直接交互时，建立 Group。每个成员保持独立上下文和专业判断，消息不必全部经入口 Agent 转发。

适合复杂协作、竞争性假设、跨专业实现和反复协商。

是否采用单 Agent、委派还是 Group，应由模型结合任务形态、成本和协作收益判断，用户可以直接指定或否决。

## Group 的网络语义

Group 是工作网节点在某个 Session 中形成的逻辑协作子网：

```text
GroupSession
├── human initiator
├── initiator agent
├── GroupMembership[]
├── GroupMessage[]
├── AgentInvocation[]
├── optional SharedTask[]
├── Artifact / shared facts
└── GroupRun[]
```

入口 Agent 只是默认对话锚点：用户没有指定接收者时由它先处理。它不拥有其他成员，不具有额外权限，也不能替其他成员修改结论或隐藏异议。

Membership、消息和 Artifact 可以在所有成员都没有 Runner 时持续存在。消息或 AgentInvocation 需要处理时，平台才为目标成员创建或恢复 WorkItem；Group 不是一组长期连接的 Agent 进程。

成员可以：

- 向任一成员发送定向消息或工作请求；
- 创建、认领、更新或放弃共享任务；
- 读取被授权的共享事实和 Artifact；
- 请求其他成员复核或反驳；
- 直接向任务发起人澄清；
- 提议邀请新的网络 Agent；
- 退出或建议替换成员。

共享任务是协作媒介，不是平台自动执行的 DAG。

## Membership 而不是模板或 Charter

Group 不依赖 Group Template，也不要求模型先生成完整 Formation JSON。成员关系以事件表达：

```text
invited
joined
declined
left
removed
replaced
```

每个 Membership 记录 Agent、版本解析策略、邀请者、原因、共享上下文范围、有效时间和授权来源。平台还为每个 GroupRun 保存成员与 AgentVersion 快照，用于恢复和审计。

成员变化是否可以立即生效，由 Collaboration Policy 决定：

- 高风险或未预授权范围：等待用户确认；
- 用户预授权的候选、预算和数据范围内：可以自动加入；
- 超过人数、成本、数据或组织边界：拒绝或升级；
- 第一期 UAT：`fixed_for_run`，本轮不变更成员。

动态治理来自可解释的策略和成员事件，而不是版本化大 Charter 文档。

## 网络中的三张图

平台区分：

1. **Discovery Graph**：工作网中谁可以被谁发现，不代表权限；
2. **Membership Graph**：当前 Group 在某一时间点有哪些成员；
3. **Interaction Graph**：真实 Message、AgentInvocation、共享任务和 Artifact 关系。

不预建第四张“应该怎样协作”的业务图。UI 可以从真实事件生成动态图，但不能用建议边限制模型。

## 信息共享

Group 采用“按需共享、可寻址交互”：

- 定向消息只唤醒接收者；
- Group 消息可以被全体成员按权限读取；
- Tool/Agent 结果、共享任务、Artifact、测试、决定摘要和风险可以共享；
- 成员根据当前任务按需检索，不把全量 Transcript 塞入每次 Prompt；
- Instructions、私有 Memory、完整 Prompt、凭证和原始 Chain-of-Thought 不共享；
- 新成员只获得明确的 Bootstrap 与之后允许读取的共享事实。

建议可见性为 `private | direct | group | user`，实际投影必须同时满足数据产生者、原始用户和平台策略。

## 专业分歧

Group 不要求虚假共识。成员可以保留反证、风险和不同结论，并直接让用户查看。默认对话锚点在汇总时必须忠实表达影响结果的重要分歧，不能覆盖其他成员记录。

平台不通过通用 Evaluator 裁决专业争论。可以按业务引入评审 Agent、确定性测试或人工决定，但这些都是网络中的可见参与行为，而不是隐藏 Supervisor。

## Group 如何停止

每个 Agent 根据自己的 Turn、任务和环境反馈自然停止或等待。GroupRun 关联 Run 的状态来自真实事件：

- 是否仍有运行中的 Tool/Agent Invocation；
- 是否有人在等待用户或审批；
- 是否仍有明确的共享任务处于进行中；
- 是否被用户取消或达到硬预算。

每个 GroupRun 固定一个 `reporter_agent_id`，默认取本轮入口 Agent。成员空闲且没有 pending action 时，持久化 Coordinator 通过唯一 summary epoch 创建/恢复 Reporter WorkItem；汇总提交并二次确认仍无 pending 后才完成一对一 Run。Reporter 不可用时等待用户从本轮成员中替换。该责任不增加 Reporter 的 Tool、数据或管理权限，也不形成永久 Supervisor。

用户也可以要求任何成员交付阶段性消息。真实测试和 Artifact 决定结果能否标记为 verified，不要求通用 `OutcomeClaim` 或 LLM Evaluator。

## 人与 Agent 社会

任务发起人是 Group 的人类参与者，可以直接进入任一成员上下文、纠正目标、批准行动和结束协作。

某个 Agent 还可以联系 Group 外工作人员、业务责任人或审批人。被联系者不会因此成为 Agent 节点或自动获得 Group 历史；其答复只进入授权的等待上下文。

## 身份和权限

每次 Agent 行动继续代表 Run 创建时固定的内部 `tenant_id + account_id`：

```text
original principal authority
∩ current AgentVersion capabilities
∩ current session permission mode
∩ membership / sharing boundary
∩ tool and enterprise policy
```

Agent 的信誉和历史合作可以影响推荐排序，不能产生数据或 Tool 权限。另一个 Agent 的服务凭证也不能为调用者提权。

注册、发现、邀请、调用和授权是五个独立事实：发布 AgentCard 不自动进入所有目录，进入目录不自动允许建组，Membership 不自动扩大数据或 Tool 权限。跨域后还必须证明远程运营方身份、Endpoint 控制权和委托链；这些联邦能力通过 Gateway 增加，不修改平台内 Agent 的身份模型。

跨域 AgentCard、Message、Artifact、状态和错误都作为不可信输入处理，继续经过内容边界、Prompt Injection 防护、数据分级、出站策略和审计；网络可达性不能升级为业务信任。

## Agent 成长

```text
运行事实 / 用户反馈 / UAT
→ 可复用经验候选
→ Instructions / Skill / Memory / Tool 改进
→ Builder 或 Owner 审核
→ 新 AgentVersion
→ 新的独立与协作 Eval
```

个人偏好可以在用户私有边界内持续学习；企业专业能力的发布仍应可审查、可回滚和可追溯。

## 第一期与终局的关系

第一阶段使用五个专业 Agent 和确定的容量风险计算器场景：

- 个人助手单独验证 Chat；
- 项目经理验证显式 Plan 权限模式；
- 用户从工作网选择产品、开发、测试三个 Agent 建立 Group；
- GroupRun 保存三名成员的确定版本快照；
- 成员内部消息、任务、调用、返工和计划保持动态；
- 本轮禁用动态加人，只记录候选建议或拒绝；
- 后续阶段在同一 Membership 模型上开启用户确认和预授权加入。

第一期不需要 Group 模板、角色槽位、Charter Revision 或永久 Supervisor。它验证的是最小动态社会骨架，而不是把社会固化成模板。

## 终局底线

- 不让中央 Agent 拥有所有专业判断；
- 不用 Workflow 模板定义开放协作；
- 不让发现等于授权；
- 不把成员锁死在 Group 数据结构中；
- 不让入口 Agent 成为永久 Supervisor；
- 不让 Group 复制或修改成员 Agent；
- 不共享私有 Prompt、Memory、凭证和 Chain-of-Thought；
- 不用第一阶段固定 UAT 边界限制未来动态加入；
- 不让多 Agent 数量替代单 Agent 专业质量；
- 不为平台内每个 Agent 复制公网寻址和常驻服务；
- 不让跨域协议成为平台内部 Agent Runtime 的前置依赖。
