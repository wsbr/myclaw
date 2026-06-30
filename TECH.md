# MyClaw 技术设计文档

> 本文档是工程实现蓝图，与 PRODUCT.md 配套阅读。

---

## 目录

1. [项目目录结构](#1-项目目录结构)
2. [数据库设计（完整 DDL）](#2-数据库设计)
3. [API 设计](#3-api-设计)
4. [后端服务层设计](#4-后端服务层设计)
5. [Agent Runtime 执行流程](#5-agent-runtime-执行流程)
6. [前端页面与组件设计](#6-前端页面与组件设计)
7. [基础设施配置](#7-基础设施配置)
8. [实现路线图](#8-实现路线图)

---

## 1. 项目目录结构

```
myclaw/
├── docker-compose.yml
├── docker-compose.override.yml      # 开发环境覆盖（热重载）
├── .env.example
├── PRODUCT.md
├── TECH.md
│
├── backend/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── alembic.ini
│   ├── alembic/
│   │   ├── env.py
│   │   └── versions/
│   └── app/
│       ├── main.py                  # FastAPI app 入口
│       ├── config.py                # 配置（从环境变量读取）
│       ├── database.py              # AsyncEngine + SessionLocal
│       ├── deps.py                  # FastAPI 依赖注入（get_db, get_current_user）
│       │
│       ├── models/                  # SQLAlchemy ORM 模型
│       │   ├── __init__.py
│       │   ├── user.py
│       │   ├── agent.py
│       │   ├── tool.py
│       │   ├── skill.py
│       │   ├── mcp_server.py
│       │   ├── model_provider.py
│       │   ├── task.py
│       │   ├── session.py
│       │   ├── runner.py
│       │   ├── span.py
│       │   └── orchestration.py
│       │
│       ├── schemas/                 # Pydantic v2 请求/响应模型
│       │   ├── auth.py
│       │   ├── agent.py
│       │   ├── tool.py
│       │   ├── skill.py
│       │   ├── mcp_server.py
│       │   ├── model_provider.py
│       │   ├── task.py
│       │   ├── session.py
│       │   ├── runner.py
│       │   ├── span.py
│       │   └── orchestration.py
│       │
│       ├── api/                     # FastAPI 路由层（薄层，只做校验和转发）
│       │   ├── __init__.py
│       │   ├── router.py            # 总路由注册
│       │   ├── auth.py
│       │   ├── agents.py
│       │   ├── tools.py
│       │   ├── skills.py
│       │   ├── mcp_servers.py
│       │   ├── model_providers.py
│       │   ├── tasks.py
│       │   ├── sessions.py
│       │   ├── runners.py
│       │   └── admin.py
│       │
│       ├── services/                # 业务逻辑层
│       │   ├── auth_service.py
│       │   ├── agent_service.py
│       │   ├── tool_service.py
│       │   ├── skill_service.py
│       │   ├── mcp_service.py
│       │   ├── task_service.py
│       │   └── session_service.py
│       │
│       ├── runtime/                 # Agent 执行引擎
│       │   ├── __init__.py
│       │   ├── agent_runtime.py     # 核心执行器
│       │   ├── context_builder.py   # 构建 LLM 上下文
│       │   ├── tracer.py            # Span 记录器
│       │   ├── plan_executor.py     # Plan 模式执行
│       │   └── group_orchestrator.py # Group 模式编排
│       │
│       ├── core/                    # 核心抽象与注册表
│       │   ├── model_hub/
│       │   │   ├── base.py          # BaseLLM 接口
│       │   │   ├── anthropic.py     # Anthropic Adapter
│       │   │   ├── openai.py        # OpenAI Adapter
│       │   │   ├── ollama.py        # Ollama Adapter
│       │   │   └── registry.py      # Provider 注册表
│       │   ├── tool_registry/
│       │   │   ├── base.py          # BaseTool 接口
│       │   │   ├── registry.py
│       │   │   └── builtin/         # 内置工具实现
│       │   │       ├── create_task.py
│       │   │       ├── search_tasks.py
│       │   │       ├── create_scheduled_task.py
│       │   │       ├── send_to_channel.py
│       │   │       ├── emit_event.py
│       │   │       ├── find_agents.py
│       │   │       ├── invoke_agent.py
│       │   │       └── get_agent_schema.py
│       │   ├── mcp_registry/
│       │   │   ├── registry.py
│       │   │   └── client.py        # MCP 协议客户端
│       │   ├── memory/
│       │   │   ├── base.py
│       │   │   ├── short_term.py    # Session 内记忆（Redis）
│       │   │   └── long_term.py     # 跨 Session 记忆（MySQL）
│       │   ├── channels/
│       │   │   ├── base.py          # BaseChannel 接口
│       │   │   ├── dispatcher.py    # 渠道分发器
│       │   │   ├── platform_ui.py   # WebSocket 推送
│       │   │   ├── feishu.py
│       │   │   ├── wechat.py
│       │   │   ├── telegram.py
│       │   │   └── webhook.py
│       │   └── events/
│       │       ├── bus.py           # 事件总线（Redis Pub/Sub）
│       │       └── subscriber.py
│       │
│       └── workers/                 # Celery 任务
│           ├── celery_app.py
│           ├── task_worker.py       # 异步 Task 执行
│           └── scheduled_worker.py  # 定时 Task 调度
│
├── frontend/
│   ├── Dockerfile
│   ├── package.json
│   ├── tsconfig.json
│   ├── vite.config.ts
│   └── src/
│       ├── main.tsx
│       ├── App.tsx
│       ├── api/                     # API 客户端（axios + react-query）
│       │   ├── client.ts
│       │   ├── auth.ts
│       │   ├── agents.ts
│       │   ├── tasks.ts
│       │   ├── sessions.ts
│       │   └── admin.ts
│       ├── store/                   # Zustand 状态
│       │   ├── auth.ts
│       │   └── ui.ts
│       ├── pages/
│       │   ├── Login/
│       │   ├── Dashboard/
│       │   ├── Agents/
│       │   │   ├── List/
│       │   │   └── Config/          # Agent 装配页（核心页面）
│       │   ├── Session/             # 对话页
│       │   ├── Tasks/               # Task 看板
│       │   ├── Trace/               # Trace 查看器
│       │   ├── Orchestration/       # Group 编排图页
│       │   └── Admin/               # 管理员页面
│       └── components/
│           ├── AgentConfigForm/     # Agent 装配表单
│           ├── ChatWindow/          # 对话窗口
│           ├── TaskBoard/           # Task 状态看板
│           ├── TraceViewer/         # Span 树可视化
│           ├── OrchestrationGraph/  # React Flow 编排图
│           └── PromptEditor/        # Monaco 提示词编辑器
│
└── nginx/
    └── nginx.conf
```

---

## 2. 数据库设计

### 2.1 完整 DDL

```sql
-- ============================================================
-- 用户与认证
-- ============================================================

CREATE TABLE users (
    id          VARCHAR(36)  PRIMARY KEY DEFAULT (UUID()),
    email       VARCHAR(255) NOT NULL UNIQUE,
    username    VARCHAR(100) NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    role        ENUM('admin', 'user') NOT NULL DEFAULT 'user',
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at  DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE api_tokens (
    id          VARCHAR(36)  PRIMARY KEY DEFAULT (UUID()),
    user_id     VARCHAR(36)  NOT NULL,
    agent_id    VARCHAR(36),                    -- NULL 表示用户级别 token
    name        VARCHAR(100) NOT NULL,
    token_hash  VARCHAR(255) NOT NULL UNIQUE,
    last_used_at DATETIME(3),
    expires_at  DATETIME(3),
    created_at  DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


-- ============================================================
-- 模型 Provider
-- ============================================================

CREATE TABLE model_providers (
    id          VARCHAR(36)  PRIMARY KEY DEFAULT (UUID()),
    name        VARCHAR(100) NOT NULL,           -- "Anthropic", "OpenAI"
    provider    VARCHAR(50)  NOT NULL,           -- "anthropic" | "openai" | "ollama"
    base_url    VARCHAR(500),                    -- Ollama 等自托管时使用
    api_key_encrypted VARCHAR(1000),             -- AES 加密存储
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    created_by  VARCHAR(36)  NOT NULL,           -- admin user id
    created_at  DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    FOREIGN KEY (created_by) REFERENCES users(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE available_models (
    id              VARCHAR(36)  PRIMARY KEY DEFAULT (UUID()),
    provider_id     VARCHAR(36)  NOT NULL,
    model_id        VARCHAR(200) NOT NULL,       -- "claude-sonnet-4-6"
    display_name    VARCHAR(200) NOT NULL,
    max_tokens      INT          NOT NULL DEFAULT 8192,
    supports_tools  BOOLEAN      NOT NULL DEFAULT TRUE,
    supports_streaming BOOLEAN   NOT NULL DEFAULT TRUE,
    cost_input_per_1m DECIMAL(10,4) DEFAULT 0,  -- USD per 1M tokens
    cost_output_per_1m DECIMAL(10,4) DEFAULT 0,
    is_active       BOOLEAN      NOT NULL DEFAULT TRUE,
    FOREIGN KEY (provider_id) REFERENCES model_providers(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


-- ============================================================
-- Tool / Skill / MCP Registry
-- ============================================================

CREATE TABLE tools (
    id          VARCHAR(36)  PRIMARY KEY DEFAULT (UUID()),
    name        VARCHAR(100) NOT NULL UNIQUE,
    display_name VARCHAR(200) NOT NULL,
    description TEXT         NOT NULL,
    input_schema JSON        NOT NULL,           -- JSON Schema
    output_schema JSON,
    is_builtin  BOOLEAN      NOT NULL DEFAULT FALSE,
    implementation_key VARCHAR(200),             -- 内置工具的 Python 路径
    version     VARCHAR(20)  NOT NULL DEFAULT '1.0.0',
    is_active   BOOLEAN      NOT NULL DEFAULT TRUE,
    created_by  VARCHAR(36)  NOT NULL,
    created_at  DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    FOREIGN KEY (created_by) REFERENCES users(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE skills (
    id          VARCHAR(36)  PRIMARY KEY DEFAULT (UUID()),
    name        VARCHAR(100) NOT NULL UNIQUE,
    display_name VARCHAR(200) NOT NULL,
    description VARCHAR(500) NOT NULL,
    content     LONGTEXT     NOT NULL,           -- 提示词内容
    version     VARCHAR(20)  NOT NULL DEFAULT '1.0.0',
    is_active   BOOLEAN      NOT NULL DEFAULT TRUE,
    created_by  VARCHAR(36)  NOT NULL,
    created_at  DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at  DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
    FOREIGN KEY (created_by) REFERENCES users(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE mcp_servers (
    id              VARCHAR(36)  PRIMARY KEY DEFAULT (UUID()),
    name            VARCHAR(100) NOT NULL,
    display_name    VARCHAR(200) NOT NULL,
    description     TEXT,
    endpoint        VARCHAR(500) NOT NULL,       -- http://mcp.internal:8080
    auth_type       ENUM('none', 'bearer', 'api_key') NOT NULL DEFAULT 'none',
    auth_config_encrypted JSON,                  -- 加密的认证配置
    health_check_url VARCHAR(500),
    capability_summary TEXT,
    is_active       BOOLEAN      NOT NULL DEFAULT TRUE,
    created_by      VARCHAR(36)  NOT NULL,
    created_at      DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    FOREIGN KEY (created_by) REFERENCES users(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


-- ============================================================
-- Agent 蓝图
-- ============================================================

CREATE TABLE agents (
    id              VARCHAR(36)  PRIMARY KEY DEFAULT (UUID()),
    owner_id        VARCHAR(36)  NOT NULL,
    name            VARCHAR(200) NOT NULL,
    description     TEXT         NOT NULL,
    avatar_url      VARCHAR(500),
    status          ENUM('draft', 'active', 'inactive') NOT NULL DEFAULT 'draft',
    version         INT          NOT NULL DEFAULT 1,

    -- 执行模式（JSON 数组: ["chat", "plan", "group"]）
    modes           JSON         NOT NULL DEFAULT '["chat"]',

    -- 模型配置
    model_config    JSON         NOT NULL,
    -- {
    --   "provider_id": "xxx",
    --   "model_id": "claude-sonnet-4-6",
    --   "temperature": 0.7,
    --   "max_tokens": 8192,
    --   "streaming": true,
    --   "extended_thinking": false
    -- }

    -- 系统提示词（分区块）
    system_prompt_blocks JSON    NOT NULL DEFAULT '{}',
    -- {
    --   "identity": "你是...",
    --   "instructions": "...",
    --   "context": "...",
    --   "output_format": "..."
    -- }

    -- 上下文策略
    context_strategy JSON        NOT NULL DEFAULT '{"window_type":"sliding","max_tokens":128000}',

    -- 记忆配置
    memory_config   JSON         NOT NULL DEFAULT '{"short_term":true,"long_term":{"enabled":false}}',

    -- 沙箱配置
    sandbox_config  JSON         NOT NULL DEFAULT '{"enabled":false}',

    -- 支持的输出渠道
    supported_channels JSON      NOT NULL DEFAULT '["platform_ui"]',

    -- Group 模式配置
    group_config    JSON,
    -- {
    --   "enabled": true,
    --   "capability_manifest": [...],
    --   "event_manifest": [...],
    --   "max_depth": 5,
    --   "max_fanout": 10,
    --   "timeout_seconds": 300
    -- }

    -- 统计告警配置
    alert_config    JSON,

    created_at      DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at      DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
    FOREIGN KEY (owner_id) REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE INDEX idx_agents_owner ON agents(owner_id);
CREATE INDEX idx_agents_status ON agents(status);

-- Agent 版本快照（每次保存时追加）
CREATE TABLE agent_versions (
    id          VARCHAR(36)  PRIMARY KEY DEFAULT (UUID()),
    agent_id    VARCHAR(36)  NOT NULL,
    version     INT          NOT NULL,
    snapshot    JSON         NOT NULL,           -- agents 表某版本的完整快照
    created_at  DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    UNIQUE KEY uq_agent_version (agent_id, version),
    FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Agent 与 Tool 关联
CREATE TABLE agent_tools (
    agent_id    VARCHAR(36)  NOT NULL,
    tool_id     VARCHAR(36)  NOT NULL,
    config      JSON,                            -- 覆盖 tool 的默认配置
    sort_order  INT          NOT NULL DEFAULT 0,
    PRIMARY KEY (agent_id, tool_id),
    FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE,
    FOREIGN KEY (tool_id)  REFERENCES tools(id)  ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Agent 与 Skill 关联
CREATE TABLE agent_skills (
    agent_id    VARCHAR(36)  NOT NULL,
    skill_id    VARCHAR(36)  NOT NULL,
    sort_order  INT          NOT NULL DEFAULT 0,
    PRIMARY KEY (agent_id, skill_id),
    FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE,
    FOREIGN KEY (skill_id) REFERENCES skills(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Agent 与 MCP Server 关联
CREATE TABLE agent_mcp_servers (
    agent_id        VARCHAR(36)  NOT NULL,
    mcp_server_id   VARCHAR(36)  NOT NULL,
    sort_order      INT          NOT NULL DEFAULT 0,
    PRIMARY KEY (agent_id, mcp_server_id),
    FOREIGN KEY (agent_id)      REFERENCES agents(id)      ON DELETE CASCADE,
    FOREIGN KEY (mcp_server_id) REFERENCES mcp_servers(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Agent 内置工具开关
CREATE TABLE agent_builtin_tools (
    agent_id    VARCHAR(36)  NOT NULL,
    tool_key    VARCHAR(100) NOT NULL,           -- "create_task", "search_tasks" 等
    enabled     BOOLEAN      NOT NULL DEFAULT TRUE,
    PRIMARY KEY (agent_id, tool_key),
    FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


-- ============================================================
-- Session / Runner / Span（核心可观测性）
-- ============================================================

CREATE TABLE sessions (
    id              VARCHAR(36)  PRIMARY KEY DEFAULT (UUID()),
    agent_id        VARCHAR(36)  NOT NULL,
    user_id         VARCHAR(36)  NOT NULL,
    mode            ENUM('chat', 'plan', 'group') NOT NULL,
    title           VARCHAR(500),                -- 自动生成或用户命名
    status          ENUM('active', 'completed', 'archived') NOT NULL DEFAULT 'active',
    -- Fork 来源（重跑时填充）
    fork_from_session_id VARCHAR(36),
    fork_from_runner_id  VARCHAR(36),
    fork_from_span_id    VARCHAR(36),
    metadata        JSON,
    created_at      DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at      DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
    FOREIGN KEY (agent_id) REFERENCES agents(id),
    FOREIGN KEY (user_id)  REFERENCES users(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE INDEX idx_sessions_agent_user ON sessions(agent_id, user_id);
CREATE INDEX idx_sessions_created ON sessions(created_at);

CREATE TABLE runners (
    id              VARCHAR(36)  PRIMARY KEY DEFAULT (UUID()),
    session_id      VARCHAR(36)  NOT NULL,
    agent_id        VARCHAR(36)  NOT NULL,
    agent_version   INT          NOT NULL,
    user_id         VARCHAR(36)  NOT NULL,
    -- 在 Group 模式下，多个 agent 共享 session_id，通过 runner 区分各自执行
    parent_runner_id VARCHAR(36),               -- 调用方 runner（Group 模式下）
    status          ENUM('pending','running','completed','failed','cancelled') NOT NULL DEFAULT 'pending',
    input           JSON,
    output          JSON,
    error           TEXT,
    total_tokens    INT          DEFAULT 0,
    total_cost_usd  DECIMAL(10,6) DEFAULT 0,
    started_at      DATETIME(3),
    completed_at    DATETIME(3),
    created_at      DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE,
    FOREIGN KEY (agent_id)   REFERENCES agents(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE INDEX idx_runners_session ON runners(session_id);

CREATE TABLE spans (
    id              VARCHAR(36)  PRIMARY KEY DEFAULT (UUID()),
    runner_id       VARCHAR(36)  NOT NULL,
    parent_span_id  VARCHAR(36),
    span_type       ENUM('llm_call','tool_call','agent_call','memory','event','plan_step') NOT NULL,
    name            VARCHAR(200) NOT NULL,       -- "LLM Call #1", "call:web_search"
    status          ENUM('running','completed','failed') NOT NULL DEFAULT 'running',
    -- 快照：该 span 执行时的完整上下文（按 span_type 结构不同）
    snapshot        LONGBLOB     NOT NULL,       -- gzip 压缩的 JSON
    -- 请求/响应
    request         LONGBLOB,                   -- gzip 压缩的 JSON
    response        LONGBLOB,                   -- gzip 压缩的 JSON
    -- 性能指标
    input_tokens    INT          DEFAULT 0,
    output_tokens   INT          DEFAULT 0,
    cost_usd        DECIMAL(10,6) DEFAULT 0,
    duration_ms     INT,
    error           TEXT,
    started_at      DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    completed_at    DATETIME(3),
    FOREIGN KEY (runner_id) REFERENCES runners(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE INDEX idx_spans_runner ON spans(runner_id);
CREATE INDEX idx_spans_type ON spans(span_type);


-- ============================================================
-- Task（异步执行单元）
-- ============================================================

CREATE TABLE tasks (
    id              VARCHAR(36)  PRIMARY KEY DEFAULT (UUID()),
    agent_id        VARCHAR(36)  NOT NULL,
    agent_version   INT          NOT NULL,
    user_id         VARCHAR(36)  NOT NULL,
    session_id      VARCHAR(36),                 -- 归属的 Session
    parent_task_id  VARCHAR(36),                 -- 依赖关系（完成后触发本 task）

    title           VARCHAR(500),
    status          ENUM('pending','running','waiting','completed','failed','cancelled') NOT NULL DEFAULT 'pending',
    waiting_reason  VARCHAR(500),                -- status=waiting 时的说明

    -- 触发配置
    trigger_type    ENUM('immediate','scheduled','event') NOT NULL DEFAULT 'immediate',
    cron_expr       VARCHAR(100),                -- "0 8 * * *"
    event_type      VARCHAR(200),                -- 事件触发时的事件类型
    scheduled_at    DATETIME(3),                 -- 下次执行时间

    -- 任务内容
    input           JSON,
    output          JSON,
    output_channel  VARCHAR(50)  NOT NULL DEFAULT 'platform_ui',
    celery_task_id  VARCHAR(200),                -- Celery 任务 ID

    created_at      DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    started_at      DATETIME(3),
    completed_at    DATETIME(3),
    FOREIGN KEY (agent_id)  REFERENCES agents(id),
    FOREIGN KEY (user_id)   REFERENCES users(id),
    FOREIGN KEY (session_id) REFERENCES sessions(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE INDEX idx_tasks_agent_user ON tasks(agent_id, user_id);
CREATE INDEX idx_tasks_status ON tasks(status);
CREATE INDEX idx_tasks_scheduled ON tasks(scheduled_at, status);


-- ============================================================
-- Group 模式编排图
-- ============================================================

CREATE TABLE orchestration_graphs (
    id          VARCHAR(36)  PRIMARY KEY DEFAULT (UUID()),
    session_id  VARCHAR(36)  NOT NULL UNIQUE,
    goal        TEXT         NOT NULL,
    graph_json  JSON         NOT NULL,           -- 完整图结构
    status      ENUM('draft','confirmed','running','completed','failed') NOT NULL DEFAULT 'draft',
    created_at  DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at  DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


-- ============================================================
-- 记忆
-- ============================================================

CREATE TABLE memories (
    id          VARCHAR(36)  PRIMARY KEY DEFAULT (UUID()),
    agent_id    VARCHAR(36)  NOT NULL,
    user_id     VARCHAR(36)  NOT NULL,
    scope       ENUM('user', 'agent', 'global') NOT NULL DEFAULT 'user',
    key_name    VARCHAR(200),                    -- 可选的具名记忆 key
    content     LONGTEXT     NOT NULL,
    embedding   LONGBLOB,                        -- 向量（未来扩展）
    expires_at  DATETIME(3),
    created_at  DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id)  REFERENCES users(id)  ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE INDEX idx_memories_scope ON memories(agent_id, user_id, scope);


-- ============================================================
-- 统计聚合（按天预聚合，加速看板查询）
-- ============================================================

CREATE TABLE usage_stats (
    id              VARCHAR(36)  PRIMARY KEY DEFAULT (UUID()),
    stat_date       DATE         NOT NULL,
    agent_id        VARCHAR(36)  NOT NULL,
    user_id         VARCHAR(36)  NOT NULL,
    provider        VARCHAR(50)  NOT NULL,
    model_id        VARCHAR(200) NOT NULL,
    total_runners   INT          NOT NULL DEFAULT 0,
    total_tokens    BIGINT       NOT NULL DEFAULT 0,
    total_cost_usd  DECIMAL(12,6) NOT NULL DEFAULT 0,
    error_count     INT          NOT NULL DEFAULT 0,
    UNIQUE KEY uq_stat (stat_date, agent_id, user_id, model_id),
    FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE INDEX idx_stats_date_agent ON usage_stats(stat_date, agent_id);
```

---

## 3. API 设计

所有接口前缀：`/api/v1`
认证方式：`Authorization: Bearer <jwt_token>`

### 3.1 认证

```
POST   /auth/login              { email, password } → { access_token, user }
POST   /auth/logout             → 204
GET    /auth/me                 → User
POST   /auth/refresh            → { access_token }
```

### 3.2 Agent 管理

```
GET    /agents                  → Agent[]        (按 owner 过滤，admin 看全部)
POST   /agents                  body: AgentCreate → Agent
GET    /agents/{id}             → Agent
PUT    /agents/{id}             body: AgentUpdate → Agent
DELETE /agents/{id}             → 204
GET    /agents/{id}/versions    → AgentVersion[]
POST   /agents/{id}/activate    → Agent          (draft → active)
POST   /agents/{id}/duplicate   → Agent          (复制一个 Agent)

-- Agent 关联资源
PUT    /agents/{id}/tools       body: { tool_ids, builtin_tools } → 204
PUT    /agents/{id}/skills      body: { skill_ids } → 204
PUT    /agents/{id}/mcp-servers body: { mcp_server_ids } → 204
```

### 3.3 Session & Chat

```
GET    /sessions                → Session[]      (按 agent_id / user 过滤)
POST   /sessions                body: { agent_id, mode } → Session
GET    /sessions/{id}           → Session
DELETE /sessions/{id}           → 204

-- 对话（Streaming via SSE）
POST   /sessions/{id}/chat      body: { message, channel? }
                                → SSE stream: span events + final response

-- 会话重跑
POST   /sessions/{id}/fork      body: { runner_id, span_id, modifications }
                                → Session (新 Fork Session)
```

### 3.4 Runner & Span（可观测性）

```
GET    /sessions/{id}/runners        → Runner[]
GET    /runners/{id}                 → Runner
GET    /runners/{id}/spans           → Span[]（树形结构）
GET    /spans/{id}                   → Span（含完整快照）
```

### 3.5 Task

```
GET    /tasks                   → Task[]         (按 agent_id / user / status 过滤)
POST   /tasks                   body: TaskCreate → Task
GET    /tasks/{id}              → Task
DELETE /tasks/{id}              → 204            (cancel)
GET    /tasks/{id}/runners      → Runner[]        (该 Task 的执行历史)
```

### 3.6 Plan 模式

```
POST   /sessions/{id}/plan/generate   body: { goal }
                                      → PlanPreview { plan_id, steps[] }
PUT    /sessions/{id}/plan/{plan_id}  body: { steps[] }   (用户修改 plan)
                                      → PlanPreview
POST   /sessions/{id}/plan/{plan_id}/execute → Task
```

### 3.7 Group 编排

```
POST   /sessions/{id}/group/orchestrate
    body: { goal }
    → OrchestrationGraph (draft)

PUT    /sessions/{id}/group/graph
    body: { graph_json }              (用户修改图)
    → OrchestrationGraph

POST   /sessions/{id}/group/graph/validate-substitution
    body: { node_id, new_agent_id }
    → { valid: bool, warnings: string[] }

POST   /sessions/{id}/group/execute   → Task[]

GET    /sessions/{id}/group/graph     → OrchestrationGraph (含实时状态)
```

### 3.8 资源管理（Admin + User）

```
-- Tools
GET    /tools                   → Tool[]
POST   /tools                   body: ToolCreate → Tool       (Admin)
PUT    /tools/{id}              → Tool                        (Admin)
DELETE /tools/{id}              → 204                         (Admin)

-- Skills
GET    /skills                  → Skill[]
POST   /skills                  body: SkillCreate → Skill
PUT    /skills/{id}             → Skill
DELETE /skills/{id}             → 204

-- MCP Servers
GET    /mcp-servers             → MCPServer[]
POST   /mcp-servers             body: MCPServerCreate → MCPServer
PUT    /mcp-servers/{id}        → MCPServer
DELETE /mcp-servers/{id}        → 204
POST   /mcp-servers/{id}/health → { status, latency_ms }

-- Model Providers
GET    /model-providers         → ModelProvider[]             (Admin)
POST   /model-providers         body: ProviderCreate → ModelProvider (Admin)
GET    /model-providers/{id}/models → AvailableModel[]
```

### 3.9 Admin 专用

```
GET    /admin/users             → User[]
POST   /admin/users             → User
PUT    /admin/users/{id}        → User
DELETE /admin/users/{id}        → 204
GET    /admin/stats             → 全局统计
GET    /admin/agents            → 所有用户的 Agent（管理视角）
```

### 3.10 WebSocket

```
WS /ws/sessions/{session_id}
    → 实时推送 Span 事件、Task 状态变更、Group 图节点状态

消息格式:
{
  "type": "span_started" | "span_completed" | "task_status" | "stream_chunk",
  "data": { ... }
}
```

---

## 4. 后端服务层设计

### 4.1 接口契约

**BaseLLM**

```python
class LLMResponse(BaseModel):
    content: str
    tool_calls: list[ToolCall] = []
    usage: TokenUsage
    model: str
    finish_reason: str

class BaseLLM(ABC):
    @abstractmethod
    async def chat(self, messages: list[Message], tools: list[ToolDef] | None,
                   params: LLMParams) -> LLMResponse: ...

    @abstractmethod
    async def stream(self, messages: list[Message], tools: list[ToolDef] | None,
                     params: LLMParams) -> AsyncIterator[LLMChunk]: ...

    @abstractmethod
    async def count_tokens(self, messages: list[Message]) -> int: ...
```

**BaseTool**

```python
class ToolResult(BaseModel):
    success: bool
    data: Any
    error: str | None = None

class BaseTool(ABC):
    name: str
    description: str
    input_schema: dict                           # JSON Schema

    @abstractmethod
    async def execute(self, args: dict, context: RuntimeContext) -> ToolResult: ...
```

**BaseChannel**

```python
class BaseChannel(ABC):
    channel_type: str

    @abstractmethod
    async def send(self, content: str, metadata: dict) -> bool: ...
```

### 4.2 AgentRuntime 核心流程

```python
class AgentRuntime:
    """一次 Task 执行的生命周期管理器"""

    def __init__(self, agent_snapshot: dict, context: RuntimeContext):
        self.agent = agent_snapshot
        self.context = context              # 含 session_id, runner_id, user_id
        self.tracer = Tracer(context)
        self.context_builder = ContextBuilder(agent_snapshot)
        self.model = ModelHub.get(agent_snapshot["model_config"])

    async def run(self, user_input: str) -> AsyncIterator[Event]:
        runner = await self.tracer.start_runner(user_input)

        messages = await self.context_builder.build(
            user_input=user_input,
            history=await self.get_history(),
            memory=await self.memory.load()
        )
        tools = self._resolve_tools()

        while True:
            # 开始 LLM 调用 Span
            span = await self.tracer.start_span("llm_call", snapshot={
                "messages": messages,
                "tools": tools,
                "system_prompt": self.context_builder.rendered_prompt
            })

            async for chunk in self.model.stream(messages, tools, self.agent["model_config"]):
                yield StreamEvent(chunk)

            response = self._collect_response()
            await self.tracer.end_span(span, response=response)

            if not response.tool_calls:
                break                        # 没有工具调用，结束循环

            # 执行工具调用
            for tool_call in response.tool_calls:
                tool_span = await self.tracer.start_span("tool_call", ...)
                result = await ToolRegistry.execute(tool_call, self.context)
                await self.tracer.end_span(tool_span, response=result)
                messages.append(tool_result_message(tool_call, result))

        await self.tracer.end_runner(runner)
```

### 4.3 Tracer 设计

```python
class Tracer:
    """负责记录整个执行过程的 Span 树"""

    async def start_runner(self, input: dict) -> Runner:
        # 创建 Runner 记录，状态 running
        ...

    async def start_span(self, span_type: str, snapshot: dict, parent_span_id=None) -> Span:
        # 压缩 snapshot（gzip），写入 DB
        # 通过 WebSocket 广播 span_started 事件
        ...

    async def end_span(self, span: Span, response: dict, error: str = None):
        # 更新 Span 状态、duration、response
        # 广播 span_completed 事件
        ...

    async def end_runner(self, runner: Runner, output: dict = None, error: str = None):
        # 汇总 tokens、cost，更新 Runner 状态
        # 更新 usage_stats 统计表
        ...
```

### 4.4 ContextBuilder 系统提示词渲染

```python
class ContextBuilder:
    def render_system_prompt(self, agent: dict, resolved_tools: list, resolved_skills: list,
                              group_agents: list = None) -> str:
        blocks = agent["system_prompt_blocks"]
        parts = []

        if identity := blocks.get("identity"):
            parts.append(f"# Identity\n{identity}")

        if instructions := blocks.get("instructions"):
            parts.append(f"# Instructions\n{instructions}")

        if resolved_skills:
            skill_text = "\n\n".join(f"## {s.display_name}\n{s.content}" for s in resolved_skills)
            parts.append(f"# Skills\n{skill_text}")

        if resolved_tools:
            # tools 通过 LLM tool_definition 注入，此处只注入补充说明
            tool_text = "\n".join(f"- {t.name}: {t.description}" for t in resolved_tools)
            parts.append(f"# Available Tools\n{tool_text}")

        if group_agents:
            agent_text = "\n".join(
                f"- {a['name']} ({a['id']}): {a['description']}" for a in group_agents
            )
            parts.append(f"# Collaborating Agents\n{agent_text}")

        if context := blocks.get("context"):
            parts.append(f"# Context\n{context}")

        if output_format := blocks.get("output_format"):
            parts.append(f"# Output Format\n{output_format}")

        return "\n\n---\n\n".join(parts)
```

### 4.5 Group 编排器

```python
class GroupOrchestrator:
    async def generate_graph(self, session: Session, goal: str) -> OrchestrationGraph:
        # 1. 获取所有 Group-enabled Agent 的 name + description
        group_agents = await AgentService.get_group_agents(exclude=session.agent_id)

        # 2. 调用 LLM 生成初步编排图（只用 name+description）
        draft_graph = await self._llm_plan_graph(goal, group_agents)

        # 3. 调用 get_agent_schema 获取被编排 Agent 的详细能力
        needed_agent_ids = [n["agent_id"] for n in draft_graph["nodes"]]
        schemas = await self._load_agent_schemas(needed_agent_ids)

        # 4. 再次 LLM 精化图结构（有了详细 schema）
        final_graph = await self._llm_refine_graph(draft_graph, schemas)

        # 5. 存库，状态 draft，等待用户确认
        return await OrchestrationGraphService.save(session.id, goal, final_graph)

    async def validate_substitution(self, node_id: str, new_agent_id: str,
                                     graph: dict) -> SubstitutionResult:
        node = self._find_node(graph, node_id)
        new_agent = await AgentService.get(new_agent_id)

        # LLM 判断 new_agent 是否能完成 node.step_goal
        prompt = f"""
        Current step goal: {node['step_goal']}
        New agent description: {new_agent.description}
        Capability manifest: {new_agent.group_config.get('capability_manifest')}

        Can this agent complete the step goal? Reply with valid/invalid and reason.
        """
        result = await self.model.chat([{"role": "user", "content": prompt}], ...)
        return SubstitutionResult.parse(result)

    async def execute(self, graph: OrchestrationGraph) -> list[Task]:
        # 按图的拓扑顺序创建 Tasks，设置 depends_on
        tasks = []
        for node in topological_sort(graph.graph_json["nodes"]):
            task = await TaskService.create(
                agent_id=node["agent_id"],
                input=node["input_mapping"],
                depends_on=[edge["from"] for edge in graph.graph_json["edges"] if edge["to"] == node["id"]],
                session_id=graph.session_id
            )
            tasks.append(task)
        return tasks
```

---

## 5. Agent Runtime 执行流程

### 5.1 Chat 模式

```
POST /sessions/{id}/chat
│
├── SessionService.get_or_create_runner(session_id)
├── AgentService.get_snapshot(agent_id, version)   # 取当前版本快照
├── AgentRuntime(snapshot, context).run(user_input)
│   ├── ContextBuilder.build_messages(history, memory, user_input)
│   ├── [LOOP]
│   │   ├── Tracer.start_span("llm_call", snapshot=full_context)
│   │   ├── LLM.stream(messages, tools) → SSE chunks to client
│   │   ├── Tracer.end_span(response)
│   │   ├── if tool_calls:
│   │   │   ├── for each tool_call:
│   │   │   │   ├── Tracer.start_span("tool_call")
│   │   │   │   ├── ToolRegistry.execute(tool_call)
│   │   │   │   └── Tracer.end_span(result)
│   │   │   └── append tool results to messages, continue loop
│   │   └── else: break
│   └── ChannelDispatcher.send(output, channel)
└── Tracer.end_runner(runner)
```

### 5.2 Plan 模式

```
POST /sessions/{id}/plan/generate
├── LLM 生成结构化 Plan（JSON）
└── 返回 PlanPreview 给前端

[用户预览、修改]

POST /sessions/{id}/plan/{plan_id}/execute
├── TaskService.create(trigger=immediate)  # 创建主 Task
└── Celery Worker 接管:
    ├── 按 steps 顺序执行
    ├── 每步创建子 Runner，记录 Span
    ├── WebSocket 推送步骤进度
    └── 全部完成后 ChannelDispatcher.send(output, channel)
```

### 5.3 Group 模式 Agent 间调用

**invoke_agent 内置工具实现：**

```python
class InvokeAgentTool(BaseTool):
    name = "invoke_agent"

    async def execute(self, args: dict, context: RuntimeContext) -> ToolResult:
        target_agent_id = args["agent_id"]
        task_input = args["task"]

        # 防循环检测
        if target_agent_id in context.call_chain:
            return ToolResult(success=False, error="Circular agent call detected")
        if len(context.call_chain) >= context.max_depth:
            return ToolResult(success=False, error="Max call depth exceeded")

        # 更新调用方 Task 状态为 waiting
        await TaskService.set_waiting(context.task_id, f"等待 {target_agent_id} 返回结果")

        # 创建子 Task（共享 session_id）
        sub_task = await TaskService.create(
            agent_id=target_agent_id,
            input=task_input,
            session_id=context.session_id,         # 共享 session
            parent_task_id=context.task_id,
            call_chain=context.call_chain + [target_agent_id]
        )

        # 等待子 Task 完成（轮询 Redis 状态，或 await Celery result）
        result = await TaskService.wait_for_completion(sub_task.id, timeout=context.timeout)

        await TaskService.set_running(context.task_id)
        return ToolResult(success=True, data=result)
```

### 5.4 会话重跑（Fork）

```
POST /sessions/{origin_id}/fork
  body: { runner_id, span_id, modifications }

├── 读取 origin runner 的 span 树
├── 找到 fork_point span（span_id）
├── 创建新 Session（fork_from_* 字段填充）
├── 创建新 Runner
├── 重建 fork_point 之前的 messages 上下文
│   （从 span 快照中按序重建，不重新执行）
├── 应用 modifications（修改 skill 内容、prompt 等）
└── 从 fork_point 开始执行新的 AgentRuntime
```

---

## 6. 前端页面与组件设计

### 6.1 页面清单

| 路由 | 页面 | 核心功能 |
|------|------|---------|
| `/login` | 登录 | 邮箱密码登录 |
| `/` | Dashboard | 统计概览、最近 Task、快捷入口 |
| `/agents` | Agent 列表 | 卡片展示、创建、搜索 |
| `/agents/:id/config` | Agent 配置 | 核心装配页，分 Tab |
| `/agents/:id/chat` | 对话页 | Chat/Plan/Group 对话 |
| `/tasks` | Task 看板 | 状态分组、筛选、实时更新 |
| `/sessions/:id/trace` | Trace 查看器 | Span 树、时间线、快照查看 |
| `/sessions/:id/graph` | Group 图 | React Flow 编排图 |
| `/admin/users` | 用户管理 | Admin 专用 |
| `/admin/tools` | Tool 管理 | Admin 专用 |
| `/admin/skills` | Skill 管理 | |
| `/admin/mcp-servers` | MCP 管理 | |
| `/admin/model-providers` | 模型 Provider 管理 | Admin 专用 |
| `/admin/stats` | 全局统计 | 费用、用量、错误率 |

### 6.2 Agent 配置页 Tab 结构

```
Agent Config Page
├── Tab: 基础信息    (名称、描述、头像、状态、执行模式)
├── Tab: 模型         (Provider 选择、model_id、参数调节)
├── Tab: 系统提示词   (分区块 Monaco 编辑器，实时预览合并结果)
├── Tab: 工具与能力   (Tool 多选、Skill 多选、MCP Server 多选、内置工具开关)
├── Tab: 上下文策略   (window_type、max_tokens、压缩策略)
├── Tab: 记忆         (短期/长期开关、scope、TTL)
├── Tab: 渠道         (支持渠道多选、渠道连接配置)
├── Tab: Group 配置   (能力清单编辑器、事件清单、防护参数)
├── Tab: API 令牌     (生成/删除 token)
└── Tab: 统计告警     (告警阈值配置、历史费用图表)
```

### 6.3 Trace 查看器布局

```
┌─────────────────────────────────────────────────────┐
│  Session: xxx  Runner: R1  [Fork 重跑]              │
├──────────────────┬──────────────────────────────────┤
│                  │  Span 详情面板                    │
│  Span 树（左侧） │  ┌─ 快照 ─────────────────────┐  │
│                  │  │ System Prompt               │  │
│  ▼ LLM Call #1  │  │ Messages[]                  │  │
│    ▶ Tool: search│  │ Tools[]                     │  │
│  ▼ LLM Call #2  │  └─────────────────────────────┘  │
│    ▶ Tool: calc  │  ┌─ 响应 ─────────────────────┐  │
│  ▶ LLM Call #3  │  │ Content / Tool Calls        │  │
│                  │  │ Tokens: 1234  Cost: $0.002  │  │
│                  │  └─────────────────────────────┘  │
│                  │  [在此节点编辑后重跑]             │
└──────────────────┴──────────────────────────────────┘
```

---

## 7. 基础设施配置

### 7.1 docker-compose.yml

```yaml
version: '3.9'

services:
  nginx:
    image: nginx:1.25-alpine
    ports:
      - "80:80"
    volumes:
      - ./nginx/nginx.conf:/etc/nginx/nginx.conf:ro
      - frontend_build:/usr/share/nginx/html:ro
    depends_on:
      - api
      - frontend

  frontend:
    build: ./frontend
    volumes:
      - frontend_build:/app/dist

  api:
    build: ./backend
    command: uvicorn app.main:app --host 0.0.0.0 --port 8000
    env_file: .env
    depends_on:
      mysql:
        condition: service_healthy
      redis:
        condition: service_healthy
    volumes:
      - ./backend:/app

  worker:
    build: ./backend
    command: celery -A app.workers.celery_app worker --loglevel=info --concurrency=4
    env_file: .env
    depends_on:
      - redis
      - mysql

  beat:
    build: ./backend
    command: celery -A app.workers.celery_app beat --loglevel=info
    env_file: .env
    depends_on:
      - redis

  mysql:
    image: mysql:8.0
    environment:
      MYSQL_ROOT_PASSWORD: ${MYSQL_ROOT_PASSWORD}
      MYSQL_DATABASE: ${MYSQL_DATABASE}
      MYSQL_USER: ${MYSQL_USER}
      MYSQL_PASSWORD: ${MYSQL_PASSWORD}
    volumes:
      - mysql_data:/var/lib/mysql
    healthcheck:
      test: ["CMD", "mysqladmin", "ping", "-h", "localhost"]
      interval: 5s
      timeout: 5s
      retries: 10

  redis:
    image: redis:7-alpine
    command: redis-server --requirepass ${REDIS_PASSWORD}
    volumes:
      - redis_data:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      retries: 5

volumes:
  mysql_data:
  redis_data:
  frontend_build:
```

### 7.2 nginx.conf

```nginx
events { worker_connections 1024; }

http {
    upstream api {
        server api:8000;
    }

    server {
        listen 80;

        # 前端静态资源
        location / {
            root /usr/share/nginx/html;
            try_files $uri $uri/ /index.html;
        }

        # API 代理
        location /api/ {
            proxy_pass http://api;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
        }

        # WebSocket 代理
        location /ws/ {
            proxy_pass http://api;
            proxy_http_version 1.1;
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection "upgrade";
            proxy_read_timeout 3600s;
        }
    }
}
```

### 7.3 .env.example

```bash
# Database
MYSQL_ROOT_PASSWORD=rootpassword
MYSQL_DATABASE=myclaw
MYSQL_USER=myclaw
MYSQL_PASSWORD=mypassword
DATABASE_URL=mysql+aiomysql://myclaw:mypassword@mysql:3306/myclaw

# Redis
REDIS_PASSWORD=redispassword
REDIS_URL=redis://:redispassword@redis:6379/0

# Security
SECRET_KEY=your-secret-key-min-32-chars
ENCRYPTION_KEY=your-aes-256-encryption-key

# Celery
CELERY_BROKER_URL=redis://:redispassword@redis:6379/1
CELERY_RESULT_BACKEND=redis://:redispassword@redis:6379/2

# App
DEBUG=false
CORS_ORIGINS=["http://localhost"]
```

---

## 8. 实现路线图

### Phase 1 — 脚手架与基础设施（目标：`docker compose up` 全部服务启动）

- [ ] 初始化 backend 目录，写 `requirements.txt`
- [ ] FastAPI app 骨架（`main.py`、`config.py`、`database.py`）
- [ ] JWT 认证（login / me / logout API）
- [ ] `docker-compose.yml` + `nginx.conf` + `.env.example`
- [ ] Alembic 初始化，写全量 DDL 迁移文件
- [ ] 初始化 frontend（Vite + React + TS + Ant Design Pro）
- [ ] 前端登录页 + Auth store

### Phase 2 — 数据模型与管理 API（目标：Postman 可完整 CRUD）

- [ ] 所有 SQLAlchemy 模型
- [ ] Pydantic schemas
- [ ] CRUD API：User、Agent、Tool、Skill、MCPServer、ModelProvider
- [ ] Admin 权限中间件

### Phase 3 — 执行引擎核心（目标：Chat 模式端到端跑通）

- [ ] `BaseLLM` 接口 + Anthropic Adapter + OpenAI Adapter
- [ ] `ModelHub.get()` 工厂方法
- [ ] `BaseTool` 接口 + ToolRegistry
- [ ] `ContextBuilder` 系统提示词渲染
- [ ] `Tracer` Span 树记录
- [ ] `AgentRuntime.run()` 核心循环（Chat 模式）
- [ ] SSE 流式响应
- [ ] WebSocket Span 事件推送

### Phase 4 — Task 系统与 Plan 模式（目标：异步任务完整跑通）

- [ ] Celery App 配置
- [ ] Task CRUD API + 状态机
- [ ] 内置工具：`create_task`、`create_scheduled_task`、`search_tasks`
- [ ] Celery Beat 定时调度
- [ ] Plan 模式：generate / edit / execute
- [ ] Channel Dispatcher：`platform_ui`（WebSocket）

### Phase 5 — Group 模式与 MCP（目标：多 Agent 协作跑通）

- [ ] Capability Registry（Group Agent 注册和发现）
- [ ] 内置工具：`find_agents`、`invoke_agent`、`get_agent_schema`、`emit_event`
- [ ] `GroupOrchestrator`：生成图、验证替换、执行图
- [ ] MCP Registry + MCP Client（HTTP/SSE）
- [ ] 事件总线（Redis Pub/Sub）
- [ ] 会话 Fork 重跑 API

### Phase 6 — 前端管理界面（目标：完整可用的 Web UI）

- [ ] Agent 列表页 + 创建流程
- [ ] Agent 配置页（全 Tab）+ Monaco 编辑器
- [ ] 对话页（Chat / Plan / Group 三种模式）
- [ ] Task 看板（实时状态）
- [ ] Trace 查看器（Span 树 + 快照详情）
- [ ] Group 编排图（React Flow + 自然语言修改）
- [ ] Admin 页面（用户/Tool/Skill/MCP/Model 管理）

### Phase 7 — 渠道与记忆（目标：生产可用）

- [ ] 飞书 Channel Adapter
- [ ] 微信企业号 Channel Adapter
- [ ] Telegram Channel Adapter
- [ ] Webhook Channel Adapter
- [ ] Memory Manager（短期 Redis + 长期 MySQL）
- [ ] 统计聚合 + 告警系统
- [ ] 全局统计看板
