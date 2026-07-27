# MyClaw 测试框架

面向 MyClaw 全平台的系统性测试套件，覆盖单元测试、集成测试、进程内 E2E、**真实部署全栈测试**。

## 测试金字塔

```
                    ┌─────────────────────┐
                    │  UI E2E (Playwright) │  ← 真实浏览器 + 真实部署
                    │  全部页面功能         │
                  ┌─┴─────────────────────┴─┐
                  │  Fullstack API E2E      │  ← 真实 api + worker + mock-llm
                  │  全部后端功能 + Trace    │
                ┌─┴─────────────────────────┴─┐
                │  In-process E2E             │  ← 快速回归，开发时用
                │  Plan 模式 Trace 验证        │
              ┌─┴─────────────────────────────┴─┐
              │  Integration Tests              │  ← API + DB
            ┌─┴─────────────────────────────────┴─┐
            │  Unit Tests                         │  ← 纯函数
            └─────────────────────────────────────┘
```

## 目录结构

```
test/
├── unit/                        # 单元测试
├── integration/                 # 集成测试（进程内，Mock LLM）
├── e2e/
│   ├── test_plan_execution_trace.py   # 进程内 E2E（快速回归）
│   ├── fullstack/                     # 真实部署 API 全栈测试
│   │   ├── test_stack_health.py
│   │   ├── test_auth.py
│   │   ├── test_agents.py
│   │   ├── test_admin.py
│   │   ├── test_chat.py
│   │   ├── test_plan.py
│   │   ├── test_tasks.py
│   │   └── test_trace.py
│   └── ui/                            # Playwright 浏览器测试
│       ├── tests/
│       │   ├── login.spec.ts
│       │   ├── navigation.spec.ts
│       │   ├── agents.spec.ts
│       │   ├── tasks.spec.ts
│       │   ├── admin.spec.ts
│       │   ├── session-chat.spec.ts
│       │   ├── session-plan.spec.ts
│       │   └── trace.spec.ts
│       └── playwright.config.ts
├── shared/                      # 工具库
├── docker/                      # Mock LLM 服务
├── scripts/
│   ├── run_unit.sh
│   ├── run_integration.sh
│   ├── run_e2e.sh             # 进程内 E2E（开发时快速跑）
│   └── run_fullstack.sh         # 真实部署全栈测试（提测/上线前必跑）
└── docker-compose.test.yml
```

## 功能覆盖矩阵

| 功能 / 页面 | 单元 | 集成 | 进程内 E2E | 全栈 API | UI E2E |
|------------|------|------|-----------|---------|--------|
| 登录鉴权 | - | ✅ | - | ✅ | ✅ |
| Agent 列表/配置 | - | ✅ | - | ✅ | ✅ |
| **Agent 装配（Tool/Skill/MCP/Builtin）** | - | ✅ | - | ✅ | ✅ |
| Chat 对话 | - | - | - | ✅ | ✅ |
| Plan 模式 + Trace | ✅ | ✅ | ✅ | ✅ | ✅ |
| Task 看板 | - | - | - | ✅ | ✅ |
| 用户管理 | - | - | - | ✅ | ✅ |
| Tool 管理 | - | - | - | ✅ | ✅ |
| Skill 管理 | - | - | - | - | ✅ |
| MCP 管理 | - | - | - | - | ✅ |
| 模型管理 | - | - | - | ✅ | ✅ |
| Trace 可视化 | - | - | - | ✅ | ✅ |
| Group 编排 | - | - | - | - | - |

## 快速开始

### 1. 安装依赖

需要 **Python 3.10+**（推荐 3.12）。

```bash
cd test
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.test.example .env.test
```

### 2. 开发时快速回归（进程内，无需部署应用）

```bash
# 启动测试基础设施
docker compose -f docker-compose.test.yml up -d mysql-test redis-test

# 跑单元 + 集成 + 进程内 E2E
./scripts/run_unit.sh
./scripts/run_integration.sh
./scripts/run_e2e.sh
```

### 3. 提测 / 上线前：真实部署全栈测试

```bash
# 一键：部署全栈 + 跑 API 全栈测试 + 跑 UI 浏览器测试
./scripts/run_fullstack.sh
```

这会：
1. `docker compose --profile fullstack up --build` 部署完整栈：
   - `mysql-test` (:3307)
   - `redis-test` (:6380)
   - `mock-llm` (:9090)
   - `api-test` (:8001) — 真实 FastAPI
   - `worker-test` — 真实 Celery Worker（异步执行）
   - `frontend-test` (:8081) — 真实前端
2. 跑 `e2e/fullstack/` — 通过 HTTP 测全部后端功能
3. 跑 `e2e/ui/` — Playwright 浏览器测全部页面

### 分步执行

```bash
# 只部署全栈
docker compose -f docker-compose.test.yml --profile fullstack up -d --build
./scripts/wait_for_stack.sh

# 只跑 API 全栈测试
pytest e2e/fullstack/ -m fullstack -v

# 只跑 UI 测试
cd e2e/ui && npm install && npx playwright install chromium && npm test
```

## 测试分层说明

| 层级 | 标记 | 部署方式 | 适用场景 |
|------|------|---------|---------|
| Unit | `@pytest.mark.unit` | 无 | 纯函数逻辑 |
| Integration | `@pytest.mark.integration` | MySQL+Redis | API 契约 |
| E2E (in-process) | `@pytest.mark.e2e` | MySQL+Redis，进程内 app | 开发时快速回归 |
| **Fullstack API** | `@pytest.mark.fullstack` | **完整 Docker 栈** | 提测门禁 |
| **UI E2E** | Playwright | **完整 Docker 栈 + 浏览器** | 上线门禁 |

## 全栈测试验证内容

### API 全栈（真实 Worker 异步执行）

- Auth 登录 / 鉴权
- Agent CRUD + 激活
- Admin 资源（Tools / Skills / MCP / Models / Users）
- Chat 模式 SSE 流式 + Trace
- Plan 模式完整链路 + TraceValidator
- Task 看板创建 + 异步完成
- Trace API 返回解码后的 Span 数据

### UI 全栈（真实浏览器）

- 登录页（成功 / 失败）
- 侧边栏导航（全部 7 个页面可达）
- Agent 列表 / 配置
- Chat 对话（发消息 → 收到回复）
- Plan 模式（生成计划 → 确认执行 → 等待完成）
- Task 看板渲染
- Admin 各管理页面
- Trace 可视化页面

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `MYCLAW_FULLSTACK_API_URL` | `http://localhost:8001` | 部署的 API 地址 |
| `MYCLAW_FULLSTACK_UI_URL` | `http://localhost:8081` | 部署的前端地址 |
| `MYCLAW_MOCK_LLM_URL` | `http://localhost:9090` | Mock LLM 地址 |

## 推荐工作流

```
开发中     → ./scripts/run_e2e.sh          (秒级，进程内)
提测前     → ./scripts/run_fullstack.sh    (分钟级，真实部署)
上线前     → run_fullstack.sh + 人工冒烟    (全栈自动 + 人工补充)
```
