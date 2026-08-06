# Agent-native Harness 调研与架构判断

> 首次调研：2026-08-04；最近复核：2026-08-06。本文记录外部产品机制、UAT 证据和架构判断，作为设计决策依据；产品语义仍以 `PRODUCT.md` 与 `docs/modules/` 为准。

## 1. 为什么重新调研

上一版目标文档虽然删除了 `finalize_response`，但又引入了 `GoalEnvelope → OutcomeClaim → 强制 Evaluator → Settlement`、`GroupCharter → Formation → Amendment` 等重对象链。它解决了部分可信性问题，却仍然假设平台应预先定义 Agent 如何开始、如何结束、如何组队和如何被验收。

本次重新调研要回答的不是“怎样设计更完整的工作流”，而是：

1. 成熟 Coding Agent 如何在普通 Chat 中处理复杂多步骤任务；
2. Plan 模式究竟是另一套执行引擎，还是交互与权限策略；
3. 多 Agent 协作是否依赖预置 Group 模板和固定流程；
4. Harness 应固化哪些确定性事实，哪些判断应留给模型；
5. 完成、停止、验证和外部验收应如何解耦。

## 2. 本项目 UAT 给出的证据

2026-08-04 小型开发组 UAT 的主要问题不是模型没有能力，而是 Harness 让模型在错误协议中工作：

- 普通自然语言任务被强制套入结构化 Conversation Completion Contract；
- `finalize_response` 让模型同时声明最终文本和“必须使用过的 Tool 类型”；
- Tool 类型被当成一次性效果，成功后从后续能力中移除；
- Plan Proposal 被冻结成 DAG，模型后续只能在平台预设节点内推进；
- Group Formation 要求模型一次性输出完整组图；
- 复杂最终结构化响应失败，会让已经完成的业务工作整体失败；
- Tool Confirmation、Agent 等待、取消和最终结算互相耦合，Run 容易永久 waiting；
- 真实九例只执行了 6/9，但模型曾形成“16/16”文字结论，说明事实验证必须来自环境，而不能来自完成信封。

可复用的工程成果包括：Invocation 持久化、Outbox、lease/fencing、Workspace、Artifact checksum、ExecutionEvent、SSE 和 OTel。这些属于可信执行内核，不应因 Harness 重构而丢失。

## 3. Codex 的机制

### 3.1 普通 Chat 本身可以规划并执行

Codex 官方最佳实践建议复杂任务先规划，但普通 Agent 会根据任务复杂度自行拆解、调用工具、更新工作计划并继续执行。App Server 甚至提供 `turn/plan/updated`，用于模型在普通执行过程中分享或修改计划。

因此，“是否规划”是 Agent 的认知决定；“是否进入 Plan 模式”是用户的交互决定。两者不能合并。

### 3.2 Plan 是实施前检查点

Codex 的 `/plan` 让 Agent 先收集上下文、澄清和提出方案，再开始实施。计划可以被展示和修订，但它不是平台编译后的业务 DAG。

### 3.3 多 Agent 是按需委派

Codex 的 Subagent 由主 Agent 根据自然语言任务或明确指令按需产生。每个 Subagent 有独立上下文，结果返回调用方；主会话聚合结果。专业 Agent 的配置用于能力和行为差异，不用于预定义整张执行图。

### 3.4 Turn、Item 与最终消息分离

Codex App Server 将 Turn、Plan、Command、File Change、Tool Call、Agent Message 分成不同 Item 生命周期。`turn/completed` 表达本轮完成，最终 `item/completed` 是该 Item 的权威状态。这里没有要求模型调用一个业务 `finalize_response` 来证明所有工作完成。

参考：

- [Codex Best Practices](https://learn.chatgpt.com/guides/best-practices)
- [Codex Subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents)
- [Codex App Server](https://developers.openai.com/codex/app-server)

## 4. Claude Code 的机制

### 4.1 Plan 是权限模式

Claude Code Plan Mode 允许读取、调查和形成计划，但禁止源码修改。用户批准计划后，原 Session 退出 Plan Mode 并继续实施。这里的关键抽象是“同一 Session 的只读权限态”，在 MyClaw 中精确映射为 `permission_mode=plan_readonly`，不是另一套 Plan Runtime。

### 4.2 Agent Team 是运行时社会关系

Claude Agent Team 在第一个 teammate 被拉起时形成。成员由自然语言目标驱动产生；模型可以提出新增 teammate，但需要用户确认。Team 由独立 Session、共享任务列表和 Mailbox 组成，成员可以直接通信、自主认领未阻塞任务，用户也可以直接进入任一成员会话。

这证明：

- Group 不需要模板才能成立；
- 专业 Agent 定义可以复用，但 Team 本身是运行时关系；
- 共享任务列表是协作工具，不是预先冻结的流程；
- 成员发现、组队、消息和任务认领可以由 Agent 驾驭；
- 当前产品仍存在恢复、任务状态和关闭等局限，多 Agent Harness 需要渐进验证。

### 4.3 Stop Hook 是可选反馈，不是完成 Tool

Claude Code 的 Stop/SubagentStop Hook 可以在 Agent 准备停止时依据外部事实返回反馈，让 Agent 继续。但 Hook 是部署方可选的生命周期扩展，并不是模型必须调用的最终业务 Tool。

参考：

- [Claude Code Permission Modes](https://code.claude.com/docs/en/permission-modes)
- [Claude Code Subagents](https://code.claude.com/docs/en/sub-agents)
- [Claude Code Agent Teams](https://code.claude.com/docs/en/agent-teams)
- [Claude Code Hooks](https://code.claude.com/docs/en/hooks)

## 5. Agent 研究给出的共同原则

Anthropic 将 Workflow 与 Agent 明确区分：Workflow 由代码预定义路径，Agent 由模型动态决定过程和 Tool。其推荐从简单 Agent Loop 开始，只有在可衡量地改善结果时才增加复杂模式。

Evaluator–Optimizer 适用于评价标准清晰、迭代确实可提升结果的任务；它不是所有 Agent Run 的通用终态协议。Coding Agent 更常用环境反馈和自动化测试作为 ground truth。

长任务 Harness 的重点是：

- 给新上下文留下高信号的进度状态；
- 让 Agent 逐步推进并从环境反馈中修正；
- 使用文件、测试、Git 和 Artifact 保存事实；
- 控制上下文污染和压缩损失；
- 不让“看起来完成”替代可验证环境状态。

参考：

- [Building Effective AI Agents](https://www.anthropic.com/engineering/building-effective-agents)
- [Effective Context Engineering for AI Agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
- [Effective Harnesses for Long-running Agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)
- [Trustworthy Agents in Practice](https://www.anthropic.com/research/trustworthy-agents)

## 6. 2026 行业 Harness 形态

官方资料显示，行业正在把三个层次逐步分开：Agent 定义、Harness loop、Runtime/Hosting，而不是默认“每个 Agent 一个常驻 IP 服务”。

### OpenAI Agents SDK：从小原语扩展到可嵌入 Sandbox Harness

[OpenAI Agents SDK](https://openai.github.io/openai-agents-python/)以 Agent、Tool、handoff/agent-as-tool、guardrail、session 和 tracing 为核心；[Runner](https://openai.github.io/openai-agents-python/running_agents/)负责模型—Tool—结果—继续循环，普通 final 且没有 Tool Call 时结束。当前 [Sandbox Agents（Beta）](https://openai.github.io/openai-agents-python/sandbox_agents/)还提供 manifest 定义的真实工作区、文件/命令能力、Sandbox lifecycle、snapshot/session state 和恢复任务的能力。

因此不能再把它描述为只有轻量 loop、没有持久工作区或恢复。更准确的边界是：它是可嵌入应用的 SDK/Harness；MyClaw 仍需证明自己的多用户持久业务状态、Outbox/lease/fencing、企业 Registry/版本、API/SSE、身份治理和 Agent Network 控制面的价值。

### Microsoft Agent Framework：明确区分 Agent、Harness 与 Workflow

[Microsoft Agent Framework](https://learn.microsoft.com/en-us/agent-framework/overview/)把能力分为 Agents、面向长多步骤任务的 opinionated Harness，以及显式图 Workflow。[HarnessAgent](https://learn.microsoft.com/en-us/agent-framework/agents/harness)已经包含计划/todo、Context Compaction、文件访问、file-based Memory、Tool Approval、Skills 和 OpenTelemetry；Workflow 用于开发者需要固定执行顺序的场景。

这支持 MyClaw 的两个判断：开放 Agent 工作与确定性 Workflow 都有价值，但不应强制互相伪装；Harness 是共享运行能力，不是每个业务 Agent 各自重写的一套循环。

### Amazon AgentCore：Harness 是配置化 loop，Runtime 是托管承载

[Amazon Bedrock AgentCore Harness 与 Runtime](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-vs-runtime.html)明确区分：Harness 由配置声明 model、instructions、tools、memory、limits并提供 orchestration loop；Runtime 承载自带代码/框架，提供隔离、扩缩、Session、认证门禁和可观测基础设施。

这与 MyClaw“AgentVersion 是配置、Runner 按需装配”的方向相容，也提高了竞争门槛：身份、并发隔离、版本、文件、可观测性和安全运行必须成为真实平台能力，不能只停留在 Agent 配置 UI。

### Google ADK：动态多 Agent 与 Workflow 并存

[Google Agent Development Kit](https://docs.cloud.google.com/gemini-enterprise-agent-platform/build/adk)同时支持 workflow agents 的可预测 pipeline 和 Agent 协调的动态路由，并覆盖多 Agent、Tool、评估和多种部署 Runtime。

这说明“Agent-native”不等于排斥 Workflow；正确边界是：开放目标使用模型驱动协作，步骤与控制要求确定时使用显式 Workflow。MyClaw 第一期选择前者，不应把这一选择写成所有企业任务的普遍答案。

### 行业对本项目的直接约束

1. 本地 Agent 不需要独立 IP/进程；逻辑 ID + Directory + 按需 Runner 是合理实现；
2. Agent 定义必须冻结可复现依赖，尤其精确 SkillVersion，而不是 Runner 启动时漂移；
3. Harness 与 Runtime 可以分层，但身份、隔离、持久状态和观测不能成为“以后再说”的生产空白；
4. 多 Agent/Group 只是执行策略，必须与单 Agent、delegation 和确定性 Workflow 做成本/质量对照；
5. 可嵌入 Sandbox Harness、Managed Harness 和企业低代码平台都已覆盖大量基础能力，MyClaw 的持久业务控制面、工作网和对等 Group 只能作为待验证差异化，不是天然护城河。

商业替代方案、用户和对照实验见[产品价值验证](../modules/product-validation.md)。

## 7. 对 MyClaw 的架构判断

### 7.1 必须保留

- AgentVersion、Model、Tool、Skill、Memory、Sandbox 的真实装配；
- Identity、Authority、Capability Snapshot 和数据隔离；
- ToolInvocation / AgentInvocation 的持久化、幂等和结果状态；
- Session、Run、Turn、等待、取消、Checkpoint、Outbox、lease 和 fencing；
- Context Assembly、Compaction、Artifact、ExecutionEvent、SSE 和 Trace；
- 高风险副作用确认、预算与硬限制；
- 确定性事实校验，例如文件是否存在、测试是否真实执行、调用是否仍 pending。

### 7.2 从核心路径移除

- 模型可调用的 `finalize_response`；
- 模型声明的 `required_tool_keys`；
- Tool 类型成功一次后自动消费；
- 普通最终消息的强制结构化 Completion Envelope；
- Chat、Plan、Group 三套独立 Runtime；
- Plan 的固定 DAG 和冻结参与者集合；
- Group 模板、模型生成的强制 Formation JSON、固定角色槽位；
- `GoalEnvelope` 作为所有执行的通用前置合同；
- `OutcomeClaim + Evaluator` 作为所有 Plan/Group 的强制结算链；
- 平台根据文字关键词推断完成或下一业务步骤。

### 7.3 新的最小核心

```text
Agent = Model + Instructions + Context/Memory + Tools + Environment

Agent Loop = Observe → Decide → Act → Observe → Adapt → Respond/Wait

Mode = Interaction Policy + Permission Policy + Collaboration Policy
```

计划是任何 Agent 都可以维护的 `WorkingPlan`；Group 是从 Agent Network 选择或发现成员后形成的运行时关系；完成首先是模型自然结束当前 Turn，质量验证则由确定性检查、可选 Hook、专项 Evaluator 或人类验收按场景组合。

## 8. 设计决策

1. 三模式共用一个 Loop、一个 Run/Turn/Invocation 内核；
2. Chat 允许 Agent 自主规划并直接行动；
3. Plan 只增加“实施前只读 + 用户批准”策略；
4. Group 成员来自 Agent Network，Group 不依赖模板；
5. 第一期固定的是 UAT Run 的成员快照，不是 Group 产品本体；
6. Agent 可维护计划、共享任务和协作关系，平台不据此自动编排业务步骤；
7. Provider 最小协议是原生 Tool Call、普通 Assistant Message 和 Stop Reason；
8. 最终自然语言不强制包进复杂 JSON；
9. 结束 Turn、Run 结算、结果验证和 UAT 验收是四个不同层次；
10. Evaluator 是可选能力，只有清晰标准与收益证明时才接入；
11. 平台只对机器事实、权限和副作用作强约束；
12. 第一期优先删除错误抽象，再增加新的持久实体。

## 9. 仍需通过 UAT 证明的假设

- 同一个 Loop 在 Chat、Plan、Group 三种策略下能够稳定运行；
- 不使用 Completion Envelope 后，三个 Provider 能稳定产生 Tool Call 与最终消息；
- WorkingPlan 的动态更新能替代冻结 DAG 而不降低用户可见性；
- 手工选定三名网络 Agent 后，可以在没有 Group 模板和预设边的情况下完成业务；
- Stop/Continuation 只依据 pending action、硬门禁和预算即可避免永久循环与过早结束；
- 精确测试和 Artifact 校验可以阻止“文字通过”，无需通用 LLM Evaluator；
- Worker 重启、审批恢复和并发协作不会重复副作用或丢失上下文。
