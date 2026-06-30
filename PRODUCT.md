# MyClaw — 企业级 Agent 装配平台

> 核心设计哲学：**灵活、干净、职责清晰**

---

## 目录

1. [产品概述](#1-产品概述)
2. [核心概念体系](#2-核心概念体系)
3. [系统架构](#3-系统架构)
4. [Agent 装配体系](#4-agent-装配体系)
5. [执行模式](#5-执行模式)
6. [执行引擎与 Task 模型](#6-执行引擎与-task-模型)
7. [Group 模式详解](#7-group-模式详解)
8. [可观测性体系](#8-可观测性体系)
9. [会话重跑机制](#9-会话重跑机制)
10. [Model Hub](#10-model-hub)
11. [Tool / Skill / MCP 体系](#11-tool--skill--mcp-体系)
12. [通知渠道](#12-通知渠道)
13. [权限与多租户](#13-权限与多租户)
14. [技术栈](#14-技术栈)
15. [部署架构](#15-部署架构)
16. [核心数据模型](#16-核心数据模型)

---

## 1. 产品概述

### 1.1 定位

MyClaw 是一个面向企业的 **Agent 装配与编排平台**。核心能力是通过后台界面动态装配任意复杂度的 Agent，并将多个 Agent 组织成协作工作网，以适配各种企业业务场景——从简单的单轮问答到跨系统的长周期复杂任务。

### 1.2 核心价值

| 价值点 | 说明 |
|--------|------|
| **动态装配** | 无需写代码，在后台 UI 完整配置一个 Agent 的所有属性 |
| **全程可观测** | 每次执行的完整链路（含模型入参/出参）全部可溯源 |
| **可编辑重跑** | 拉出历史执行，修改任意节点后 Fork 重跑 |
| **Agent 工作网** | 多个单一职责 Agent 自发现、互协作，无主从层级 |
| **异步优先** | 所有耗时任务后台执行，不阻塞用户主对话 |

### 1.3 目标用户

- **Admin（平台管理员）**：装配和管理所有 Agent，维护平台资源（Tool/Skill/MCP/Model）
- **User（业务用户）**：使用自己创建的 Agent 完成业务任务，可跨 Agent 页面切换
- **企业部署方**：以 Docker Compose 快速启动验证，具备 K8s 横向扩展能力

---

## 2. 核心概念体系

```
Agent          ← 一个装配好的智能体，有名称、描述、能力集合和执行配置
  └── Task     ← Agent 执行的一次工作单元，异步、可持久化、有归属
        └── Session    ← 一次完整的交互上下文（可跨多个 Runner）
              └── Runner     ← Session 中的一次执行调用
                    └── Span Tree  ← Runner 的完整执行快照树
```

### 2.1 概念关系说明

| 概念 | 说明 |
|------|------|
| **Agent** | 静态配置蓝图，存数据库，版本化 |
| **AgentRuntime** | Agent 被触发时动态创建的执行实例，持有执行上下文 |
| **Task** | 第一等公民。一个 Task 绑定一个 Agent、一个 User、一个触发方式、一个输出渠道 |
| **Session** | 用户与 Agent 的一次交互上下文，可包含多个 Runner（多轮对话） |
| **Runner** | Session 中某一次实际执行的调用，对应一个完整的 Span 树 |
| **Span** | Runner 中的一个执行节点（LLM 调用、Tool 调用、Agent 调用等），携带完整快照 |

---

## 3. 系统架构

```
┌─────────────────────────────────────────────────────┐
│                   Frontend (React + TS)              │
│  Agent配置  │  会话交互  │  Task看板  │  执行Trace可视化 │
└───────────────────────┬─────────────────────────────┘
                        │ HTTP / WebSocket
┌───────────────────────▼─────────────────────────────┐
│              API Gateway (FastAPI)                   │
│         认证 / 路由 / 限流 / WebSocket 推送           │
├───────────────────────┬─────────────────────────────┤
│    Admin Service      │     Agent Runtime Service    │
│  (CRUD / 装配管理)    │   (执行引擎 / 任务调度)       │
├───────────────────────┴─────────────────────────────┤
│                   Core Abstractions                  │
│  Model Hub │ Tool Registry │ Skill Registry │ MCP Registry │
│  Memory Manager │ Tracer │ Channel Dispatcher        │
├─────────────────────────────────────────────────────┤
│          MySQL（持久化）  │  Redis（状态 / 队列）      │
└─────────────────────────────────────────────────────┘
```

### 3.1 服务职责边界

| 服务 | 职责 | 不负责 |
|------|------|--------|
| API Gateway | 认证、路由、限流、WS 长连接 | 任何业务逻辑 |
| Admin Service | Agent CRUD、资源管理、用户管理 | 执行任何 Agent |
| Agent Runtime Service | Task 执行、模型调用、工具编排 | 配置管理 |
| Celery Worker | 异步 Task 消费、定时调度 | 同步响应 |

---

## 4. Agent 装配体系

一个 Agent 的完整配置项如下，全部可在后台 UI 动态配置：

### 4.1 基础信息

| 字段 | 说明 |
|------|------|
| `name` | Agent 名称 |
| `description` | Agent 职责描述（在 Group 模式下用于其他 Agent 发现） |
| `avatar` | 头像 |
| `status` | active / inactive / draft |
| `version` | 配置版本号，自动递增，历史 Task 可关联回创建时的版本 |

### 4.2 执行模式

| 模式 | 说明 |
|------|------|
| `chat` | 单轮/多轮对话，ReAct 风格，同步响应为主 |
| `plan` | 先生成执行计划并预览，用户确认后再执行，支持长任务 |
| `group` | 加入 Agent 工作网，发布能力清单，可发现和协作其他 Group Agent |

一个 Agent 可同时开启多个模式。

### 4.3 模型配置

```yaml
model:
  provider: anthropic | openai | ollama | ...
  model_id: claude-sonnet-4-6
  temperature: 0.7
  max_tokens: 8192
  top_p: 1.0
  extended_thinking: false   # Anthropic 特有
  streaming: true
```

### 4.4 系统提示词

分区块结构管理，各区块独立编辑：

```
[identity]      Agent 角色和定位
[instructions]  核心行为准则
[skills]        当前装配的 Skill 描述（自动注入）
[tools]         当前装配的 Tool 描述（自动注入）
[group]         当前可协作的 Agent 列表（Group 模式下自动注入）
[context]       背景知识、企业信息等静态上下文
[output_format] 输出格式要求
```

### 4.5 工具与能力

| 配置项 | 说明 |
|--------|------|
| `tools` | 从 Tool Registry 选配，支持多选 |
| `skills` | 从 Skill Registry 选配 |
| `mcp_servers` | 从 MCP Registry 选配外部 MCP Server |
| `built_in_tools` | 平台内置工具，按需开启（`create_task`、`create_scheduled_task`、`search_tasks`、`send_to_channel` 等） |

### 4.6 上下文策略

```yaml
context_strategy:
  window_type: sliding | summary | full
  max_tokens: 128000
  compression_threshold: 0.8    # 达到此比例时触发压缩
  compression_method: summarize | truncate
```

### 4.7 记忆策略

```yaml
memory:
  short_term: true              # 当前 Session 内的对话记忆
  long_term:
    enabled: true
    backend: mysql | vector_db
    scope: user | agent | global  # 记忆的可见范围
    ttl: 30d                    # 记忆过期时间
```

### 4.8 执行沙箱

```yaml
sandbox:
  enabled: false
  type: subprocess              # subprocess | restricted_python
  timeout: 30s
  allowed_modules: [json, re, math]
```

### 4.9 通知渠道

```yaml
channels:
  supported: [platform_ui, wechat, telegram, feishu, webhook]
  default: platform_ui
```

### 4.10 Group 模式能力清单

开启 Group 模式后需配置：

```yaml
group:
  enabled: true
  capability_manifest:
    - task_type: "获取用户画像"
      input_schema: { user_id: string }
      output_schema: { profile: object }
      description: "根据用户ID查询完整的用户画像数据"
  event_manifest:
    - event_type: "问诊完成"
      payload_schema: { user_id: string, record: object }
      description: "每次问诊结束后广播问诊单据"
```

### 4.11 API 令牌

每个 Agent 可独立配置调用令牌，外部系统通过标准 HTTP API 直接调用指定 Agent。

### 4.12 统计与告警

```yaml
observability:
  alert_on_error_rate: 0.1      # 错误率超过 10% 告警
  alert_on_latency_p99: 30s     # P99 延迟告警
  alert_on_cost_daily: 100      # 日均费用告警（美元）
  retention_days: 90            # Trace 保留天数
```

---

## 5. 执行模式

### 5.1 Chat 模式

```
用户输入
  → 构建 LLM 上下文（系统提示词 + 历史 + 用户消息）
  → LLM 响应
    → 如需工具：调用工具 → 结果注入上下文 → 再次 LLM
    → 如需咨询其他 Agent（Group 模式）：发起 Agent Request → 等待结果 → 注入上下文
  → 输出到渠道
```

### 5.2 Plan 模式

```
用户输入任务
  → LLM 生成执行计划（结构化步骤列表）
  → 前端展示计划预览
  → 用户确认 / 自然语言修改
  → 按步骤异步执行
    → 每步完成后更新 Task 状态
    → 全部完成后推送结果到指定渠道
```

**计划数据结构：**

```json
{
  "plan_id": "p_xxx",
  "goal": "分析Q3销售数据并生成报告",
  "steps": [
    { "id": "s1", "description": "从数据库拉取Q3销售数据", "tool": "db_query", "status": "pending" },
    { "id": "s2", "description": "数据清洗和聚合计算", "depends_on": ["s1"], "status": "pending" },
    { "id": "s3", "description": "生成分析报告", "depends_on": ["s2"], "status": "pending" }
  ]
}
```

### 5.3 Group 模式

见第 7 节详细说明。

---

## 6. 执行引擎与 Task 模型

### 6.1 Task 是第一等公民

Task 是平台最核心的执行单元。**没有子 Agent 的概念**——异步执行本质上是同一 Agent 配置的独立运行时实例，Task 之间通过依赖关系协作，不存在主从层级。

### 6.2 Task 数据结构

```yaml
Task:
  id: task_xxx
  agent_id: agent_xxx
  agent_version: 12             # 快照执行时的 Agent 版本
  user_id: user_xxx
  session_id: session_xxx       # 所属 Session（Group 模式下跨 Agent 共享）
  
  status: pending | running | waiting | completed | failed | cancelled
  waiting_reason: "等待开发Agent返回技术评估"   # 当 status=waiting 时填充
  
  trigger:
    type: immediate | scheduled | event
    cron: "0 8 * * *"           # 定时触发
    event_type: "问诊完成"       # 事件触发
  
  input: { ... }                # 任务输入参数
  output: { ... }               # 任务执行结果
  output_channel: wechat        # 结果推送渠道
  
  depends_on: [task_yyy]        # 依赖的其他 Task
  
  created_at: ...
  started_at: ...
  completed_at: ...
```

### 6.3 异步执行场景示例

用户说："帮我后台计算一下 Q3 利润，8 点在微信上通知我结果"

```
主对话（同步继续）
├── Task A：计算 Q3 利润（立即执行，AgentRuntime 异步实例）
│     └── 完成后触发 Task B
└── Task B：8:00 推送微信（scheduled，depends_on: Task A）

用户继续问其他问题 → Agent 正常同步回答
```

### 6.4 内置工具（Built-in Tools）

这些工具是平台提供的能力，按需在 Agent 配置中开启：

| 工具 | 功能 |
|------|------|
| `create_task(description, input, channel)` | 创建异步 Task |
| `create_scheduled_task(cron, description, input, channel)` | 创建定时 Task |
| `search_tasks(filter)` | 查询 Task 列表（面向运营 Agent 开启，C 端 Agent 不开启） |
| `get_task_status(task_id)` | 查询单个 Task 状态 |
| `send_to_channel(channel, content)` | 主动推送内容到指定渠道 |
| `emit_event(event_type, payload)` | 广播事件到 Agent 工作网 |
| `find_agents(capability_query)` | 在 Group 模式下发现可协作 Agent |
| `invoke_agent(agent_id, task, context)` | 向另一个 Agent 发起协作请求 |
| `get_agent_schema(agent_id)` | 获取 Agent 的详细能力清单 |

---

## 7. Group 模式详解

### 7.1 设计哲学

- **无主从层级**：所有 Agent 平等，不存在 Orchestrator/Worker 的上下级关系
- **单一职责**：每个 Agent 专注于一个清晰的业务领域
- **图拓扑涌现**：不预先配置 Agent 之间的连接，图结构由运行时实际调用自然形成
- **完整可观测**：整个工作网的调用图被完整记录，事后可视化

### 7.2 两种通信原语

**Request-Response（主动咨询）**

```
产品Agent（正在工作）
  → invoke_agent("开发Agent", "评估功能X的技术复杂度")
  → [等待，Task 状态变为 waiting]
  ← 开发Agent 返回结果
  → 继续执行
```

**Event / Notification（事件广播）**

```
问诊Agent（完成一次问诊）
  → emit_event("问诊完成", { user_id, record })
  → 平台事件总线广播

随访Agent（已订阅"问诊完成"事件）
  ← 接收事件
  → 自动触发：为该用户制定随访计划
```

### 7.3 Group 模式执行流程（图编排）

```
Step 1: 发现阶段
  用户提交任务 → Agent 系统提示词 [group] 区块已注入所有 Group Agent 的 name + description
  LLM 决定需要哪些 Agent

Step 2: 能力加载
  LLM 调用 get_agent_schema([agent_ids])
  获取目标 Agent 的详细 capability_manifest

Step 3: 图编排生成
  LLM 生成结构化编排图（节点 = Agent，边 = 数据/控制流）

Step 4: 用户预览与确认
  前端渲染编排图
  用户可：
    ① 自然语言修改（"把节点2换成个人助手Agent"）
       → LLM 评估替换 Agent 是否具备执行能力 → 允许或告警
    ② 可视化拖拽编辑

Step 5: 确认执行
  平台按图结构驱动 Task 创建和依赖关系
  所有 Task 共享同一个 sessionId

Step 6: 实时状态
  用户可在任意 Agent 页面查询任务进度
  状态通过配置的渠道（微信/飞书）推送
```

### 7.4 编排图数据结构

```json
{
  "graph_id": "g_xxx",
  "session_id": "s_xxx",
  "goal": "为用户制定个性化随访计划",
  "nodes": [
    {
      "id": "n1",
      "agent_id": "data_center_agent",
      "agent_name": "数据中心Agent",
      "step_goal": "获取用户画像",
      "input_mapping": { "user_id": "$.input.user_id" },
      "status": "completed"
    },
    {
      "id": "n2",
      "agent_id": "medical_consultation_agent",
      "agent_name": "问诊Agent",
      "step_goal": "获取最近问诊记录",
      "input_mapping": { "user_id": "$.input.user_id" },
      "status": "running"
    },
    {
      "id": "n3",
      "agent_id": "followup_agent",
      "agent_name": "随访Agent",
      "step_goal": "综合画像和问诊记录，制定随访计划",
      "input_mapping": {
        "profile": "$.n1.output.profile",
        "records": "$.n2.output.records"
      },
      "status": "waiting"
    }
  ],
  "edges": [
    { "from": "start", "to": ["n1", "n2"], "type": "parallel" },
    { "from": ["n1", "n2"], "to": "n3", "type": "join" }
  ]
}
```

### 7.5 防护机制

| 风险 | 防护措施 |
|------|---------|
| 循环调用 | 调用链深度追踪，检测到循环时拒绝并报错 |
| 调用深度失控 | 每个 Agent 配置 `max_depth` 限制 |
| 扇出爆炸 | 每个 Agent 配置 `max_fanout` 限制 |
| 超时 | 每个 Task 配置 `timeout`，超时自动 cancel |
| 成本失控 | Group Task 设置 `max_cost_usd` 预算上限 |

---

## 8. 可观测性体系

### 8.1 层次结构

```
Session (session_id)
  ├── 元数据：agent_id, user_id, mode, created_at
  └── Runner[] (runner_id)
        ├── 元数据：触发类型, 输入, 状态, 耗时, 总 token 数, 费用
        └── Span Tree
              ├── LLMCallSpan
              │     ├── 快照：system_prompt, messages[], tools[], skills[]
              │     ├── 请求：model, params
              │     └── 响应：content, tool_calls[], usage, latency
              ├── ToolCallSpan
              │     ├── 快照：tool_name, tool_version
              │     ├── 请求：args
              │     └── 响应：result, latency, error?
              ├── AgentCallSpan（Group 模式）
              │     ├── target_agent_id, target_session_id
              │     └── [嵌套 Runner，共享 session_id]
              ├── MemorySpan
              │     └── operation: read|write, scope, content
              └── EventSpan
                    └── event_type, payload, subscribers_notified
```

### 8.2 每个 Span 的快照内容

**LLMCallSpan 快照**（最关键）：记录 LLM 调用时的完整上下文状态，包括：
- 调用时刻的系统提示词（含所有区块的实际内容）
- 完整 messages 数组
- 所有 tool_definition
- 模型参数
- 完整的响应内容和 token 用量

这是实现"精确重跑"的数据基础——用户可以看到那次调用时 Skill 的提示词是什么，改完后重跑。

### 8.3 Group 模式下的联合 Trace

Group 任务所有参与 Agent 的 Runner 共享同一个 `session_id`，Trace 视图可以：
- 展示跨 Agent 的完整调用链
- 时间线视图：并行 Agent 的执行时间轴
- 调用图视图：实际发生的 Agent 协作拓扑图

### 8.4 实时观测

- 通过 WebSocket 向前端推送 Span 事件（流式）
- 用户可在执行中实时看到每一步的进展
- Task 状态变化实时更新

---

## 9. 会话重跑机制

### 9.1 Fork 模型

重跑不是在原 Session 上修改，而是创建一个**有明确来源引用**的新 Session：

```
原始 Session S1
  Runner R1
    Span: [LLM调用1] → [Tool调用1] → [LLM调用2*] → [LLM调用3] → [Tool调用2]
                                            ↑
                                     用户修改了此处的 Skill 提示词

Fork → 新 Session S2
  fork_from: { session_id: S1, runner_id: R1, span_id: span_llm_2 }
  Runner R2
    继承上下文：LLM调用1 + Tool调用1 的结果作为初始上下文
    重新执行：从 LLM调用2 开始，使用修改后的 Skill
    新产生：[LLM调用2'] → [LLM调用3'] → [Tool调用2']
```

### 9.2 重跑操作步骤

1. 用户在 Trace 视图中选择某个 Span
2. 修改该 Span 的配置（Skill 提示词、Tool 参数、上下文内容等）
3. 点击"从此处重跑"
4. 平台创建 Fork Session，继承修改点之前的所有 Span 快照作为上下文
5. 执行新的 Runner，产生新的 Span 树
6. 前端展示新旧 Session 的对比视图

### 9.3 Group 模式重跑

在 Group 任务中，如果某个 Agent 的某个 Span 被修改重跑，**从该节点开始的整个下游编排图全部重跑**——作为一个新任务提交，因为下游 Agent 的输入依赖上游结果，局部重跑会导致数据不一致。

---

## 10. Model Hub

### 10.1 统一抽象接口

```python
class BaseLLM:
    async def chat(self, messages, tools, params) -> LLMResponse
    async def stream(self, messages, tools, params) -> AsyncIterator[LLMChunk]
    async def count_tokens(self, messages) -> int
```

所有模型 Adapter 实现此接口，Agent Runtime 只依赖接口，不感知具体模型。

### 10.2 支持的 Provider

| Provider | 接入方式 |
|----------|---------|
| Anthropic (Claude) | 原生 SDK，支持 Extended Thinking、Tool Use |
| OpenAI / Azure OpenAI | 官方 SDK |
| Ollama | OpenAI 兼容接口 |
| 其他兼容 OpenAI 接口的服务 | 通用 Adapter |

### 10.3 Model 配置管理

- Admin 在后台录入 Provider API Key（加密存储）
- 配置可用 Model 列表及参数范围限制
- 支持为不同 Agent 配置不同的模型和参数
- 费用统计按 Provider / Model / Agent / User 多维度聚合

---

## 11. Tool / Skill / MCP 体系

### 11.1 三者区别

| | Tool | Skill | MCP Server |
|---|------|-------|------------|
| **本质** | Python 函数，原子操作 | 提示词模板/增强 | 独立进程/服务 |
| **运行位置** | 平台进程内 | 注入系统提示词 | 外部服务 |
| **状态** | 无状态 | 无运行态 | 有状态，可持久连接 |
| **维护方** | 平台代码维护 | 平台/用户维护 | 独立部署，平台只连接 |
| **典型用途** | HTTP 请求、JSON 处理、时间计算 | 角色扮演、输出规范、专业知识 | 文件系统、数据库、浏览器、代码执行 |

### 11.2 Tool Registry

- Admin 注册和管理 Tool
- Tool 有版本，Agent 配置时绑定 Tool ID（不感知版本，自动取最新；或可锁定版本）
- Tool 描述会自动注入 Agent 系统提示词 `[tools]` 区块

### 11.3 Skill Registry

- Skill 是结构化的提示词片段，有名称、描述、内容
- 装配到 Agent 后注入 `[skills]` 区块
- 可在重跑时修改 Skill 内容后对比效果

### 11.4 MCP Registry

平台**只做注册和连接，不托管 MCP 进程**：

```yaml
MCP Server 注册信息:
  name: "企业文件系统MCP"
  endpoint: "http://mcp.internal.corp.com:8080"   # 内网域名
  auth: { type: bearer, token: "..." }
  capability_summary: "读写企业内网文件服务器"
  health_check_url: "http://mcp.internal.corp.com:8080/health"
```

内部 MCP Server 使用内网域名，外部使用公网域名，平台侧逻辑完全统一，无任何特殊分支处理。

---

## 12. 通知渠道

### 12.1 渠道配置层级

渠道在 **Agent 层面**配置，Task 创建时继承：

```
Agent 配置 supported_channels: [platform_ui, wechat, feishu]
用户下达任务时指定 channel: wechat
Agent 验证 wechat ∈ supported_channels → Task.output_channel = wechat
```

如果用户请求的渠道 Agent 不支持，Agent 在任务创建时明确告知并建议可用渠道。

### 12.2 支持的渠道类型

| 渠道 | 说明 |
|------|------|
| `platform_ui` | 默认，结果展示在平台界面 |
| `wechat` | 企业微信 / 微信（通过 Webhook 或消息 API） |
| `feishu` | 飞书消息 |
| `telegram` | Telegram Bot |
| `email` | 邮件 |
| `webhook` | 自定义 HTTP 回调 |

### 12.3 渠道连接管理

Admin 后台统一管理各渠道的连接配置（Webhook URL、Bot Token 等），Agent 只选择使用哪个渠道，不感知连接细节。

---

## 13. 权限与多租户

### 13.1 角色模型

平台只有两个角色，不做复杂权限树：

| 角色 | 权限 |
|------|------|
| `admin` | 全局视图，管理所有 Agent / User / Resource |
| `user` | 只能查看和操作自己的 Agent 和 Task |

### 13.2 数据隔离

每个资源（Agent、Task、Session、Memory）携带 `owner_id`，数据库查询层统一过滤。Admin 查询不带 `owner_id` 过滤条件。

权限检查在 Service 层统一实施，不散落在各处。

### 13.3 企业落地说明

团队/部门层面的组织管理不在平台内处理，由各企业在部署时自行扩展（如通过 LDAP/SSO 集成，或在 Admin 层面手动分配）。

---

## 14. 技术栈

### 14.1 后端

| 层次 | 技术选型 | 理由 |
|------|---------|------|
| Web 框架 | FastAPI + Uvicorn | 原生 async，自动 OpenAPI 文档，高性能 |
| ORM | SQLAlchemy 2.0 (async) | 成熟稳定，支持异步 |
| 数据库迁移 | Alembic | 与 SQLAlchemy 配套 |
| 异步任务 | Celery + Redis | Task 异步执行、定时调度 |
| 数据验证 | Pydantic v2 | 类型安全，FastAPI 原生集成 |
| WebSocket | FastAPI WebSocket | 实时 Span 推送 |
| 认证 | JWT + Redis（token 黑名单） | 无状态，支持主动失效 |

### 14.2 前端

| 层次 | 技术选型 | 理由 |
|------|---------|------|
| 框架 | React 18 + TypeScript | 生态成熟，类型安全 |
| UI 组件库 | Ant Design Pro | 企业级 UI 组件完善 |
| 图编辑 | React Flow | 编排图可视化和拖拽编辑 |
| 代码编辑器 | Monaco Editor | 系统提示词、Skill 编辑 |
| 状态管理 | Zustand | 轻量，够用 |
| HTTP 客户端 | Axios + React Query | 缓存和状态管理 |

### 14.3 基础设施

| 组件 | 选型 |
|------|------|
| 关系数据库 | MySQL 8.0 |
| 缓存 / 队列 | Redis 7.x |
| 反向代理 | Nginx |
| 容器化 | Docker + Docker Compose |

---

## 15. 部署架构

### 15.1 当前阶段：Docker Compose 单机部署

```yaml
# docker-compose.yml 服务清单
services:
  nginx:          # 反向代理，前端静态资源 + API 转发
  frontend:       # React 构建产物（Nginx 静态服务）
  api:            # FastAPI 主服务
  worker:         # Celery Worker（异步 Task 执行）
  beat:           # Celery Beat（定时任务调度）
  mysql:          # MySQL 8.0
  redis:          # Redis 7.x
```

一条命令启动全部服务：`docker compose up -d`

### 15.2 K8s 就绪设计原则

当前设计决策已考虑未来横向扩展，无需重写代码：

| 原则 | 实现方式 |
|------|---------|
| **服务无状态** | FastAPI 实例不持有任何内存状态，会话状态全在 Redis |
| **状态外置** | MySQL 和 Redis 是唯一状态存储，可替换为云托管服务 |
| **配置环境化** | 所有 host/port/key 通过环境变量注入，无硬编码 |
| **健康检查** | 所有服务提供 `/health` 端点 |
| **Worker 可横扩** | Celery Worker 无状态，直接扩副本数即可 |
| **接口契约稳定** | 服务间只通过 HTTP API 和 Redis 队列通信 |

扩展路径：`docker-compose.yml` → `k8s manifests`，应用代码零修改。

---

## 16. 核心数据模型

### 16.1 主要实体关系

```
User ──< Agent >── Tool (many-to-many, via AgentTool)
                ├── Skill (many-to-many, via AgentSkill)
                ├── MCPServer (many-to-many, via AgentMCP)
                └── ModelConfig (one-to-one)

Agent ──< Task
Session ──< Runner ──< Span

Task >── Session
Task >── User
Task >── Agent
```

### 16.2 关键表结构（简化）

```sql
-- Agent 蓝图（静态配置）
agents (id, owner_id, name, description, status, version,
        modes, model_config, system_prompt_blocks,
        context_strategy, memory_config, sandbox_config,
        supported_channels, group_config, created_at)

-- Task（执行单元）
tasks (id, agent_id, agent_version, user_id, session_id,
       status, waiting_reason, trigger_type, cron_expr,
       input, output, output_channel, depends_on,
       created_at, started_at, completed_at)

-- Session
sessions (id, agent_id, user_id, mode, fork_from_session_id,
          fork_from_runner_id, fork_from_span_id, created_at)

-- Runner
runners (id, session_id, agent_id, status,
         input, output, total_tokens, total_cost_usd,
         started_at, completed_at)

-- Span
spans (id, runner_id, parent_span_id, span_type,
       snapshot, request, response,
       started_at, duration_ms, error)

-- Group 编排图
orchestration_graphs (id, session_id, goal, graph_json, status)
```

### 16.3 Span 快照存储策略

- Span 数据以 JSON 存储，完整快照（含系统提示词、完整 messages）
- 大文本字段考虑压缩存储（gzip）
- 按 `created_at` 分区，支持按 `retention_days` 配置定期清理
- 热数据（近 7 天）留 MySQL，冷数据可归档到对象存储

---

## 附录：核心术语表

| 术语 | 定义 |
|------|------|
| **Agent** | 装配好的智能体蓝图，静态配置 |
| **AgentRuntime** | Agent 被触发后创建的执行实例 |
| **Task** | 一次工作单元，有归属、状态、渠道 |
| **Session** | 一次交互上下文，可包含多个 Runner |
| **Runner** | 一次实际执行调用，对应一棵 Span 树 |
| **Span** | Span 树中的一个执行节点，携带完整快照 |
| **Tool** | 平台内置的原子函数 |
| **Skill** | 注入系统提示词的提示词片段 |
| **MCP Server** | 外部注册的 MCP 协议服务 |
| **Model Hub** | 统一的多模型接入抽象层 |
| **Capability Manifest** | Group 模式下 Agent 发布的能力声明 |
| **Event Manifest** | Group 模式下 Agent 声明会广播的事件类型 |
| **Fork Session** | 从历史执行某节点分叉重跑产生的新 Session |
| **Channel** | 任务结果的输出渠道（微信、飞书、平台 UI 等）|
