# MyClaw 测试框架

第一阶段测试分为两层：快速的进程内单元/服务测试，以及基于真实 Docker 部署的全栈验收。测试对象只包含当前产品模型（Chat、Plan、Group、Agent、Tool、Skill、Model、Policy、Sandbox、Memory、Artifact、Channel 与 Trace），不再包含旧 Task、MCP、Celery 或旧 Orchestration 运行时。

## 目录

```text
test/
├── unit/backend/                       # SQLite 进程内领域与 API 测试
├── e2e/fullstack/
│   ├── conftest.py                     # 真实 HTTP 客户端与验收世界装配
│   ├── test_phase_one_acceptance.py    # 七个第一阶段 API/Worker 端到端验收场景
│   └── test_ui_acceptance.py           # Playwright Chat/Plan/Group/管理面验收
├── docker/mock_llm_server.py           # OpenAI-compatible 确定性模型
├── docker-compose.test.yml             # 隔离的真实部署栈
└── scripts/
    ├── run_unit.sh
    ├── run_fullstack.sh
    └── run_all.sh
```

## 覆盖范围

| 能力 | 单元/服务测试 | 真实部署 API 验收 | Playwright UI |
| --- | --- | --- | --- |
| 账号、角色、企业身份断言、userId 变更 | ✅ | ✅ | 登录入口与管理面 |
| Agent 版本、装配、API Key、冻结版本 | ✅ | ✅ | Agent 列表与会话入口 |
| Chat、确认、重连、Runner 恢复 | ✅ | ✅ | Chat 完整往返 |
| Tool 即时治理、Sandbox、文件与 Artifact | ✅ | ✅ | 管理面与 Timeline |
| Plan 图编辑、冻结、并行节点与结算 | ✅ | ✅ | 提案、确认、完成 |
| Group Formation、澄清、协作与历史检索 | ✅ | ✅ | Formation、确认、GroupRun |
| Memory、History、Channel Delivery、Trace | ✅ | ✅ | 管理面与 Timeline |
| OpenAPI 第一阶段关键路径 | — | ✅ | — |

## 准备环境

需要 Docker、Docker Compose 和 Python 3.12。

```bash
cd test
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.test.example .env.test
```

## 执行

快速回归（不需要启动 Docker）：

```bash
./scripts/run_unit.sh
```

Docker Mock 模型快速回归：

```bash
./scripts/run_fullstack.sh
```

Mock 只用于确定性快速回归，不是第一阶段最终验收依据。最终验收必须显式使用真实模型模式，并从当前 shell 或 CI Secret 注入凭证：

```bash
read -r -s MYCLAW_REAL_LLM_API_KEY
export MYCLAW_REAL_LLM_API_KEY
export MYCLAW_LLM_MODE=real
export MYCLAW_REAL_LLM_ENDPOINT=https://example.invalid/compatible/v1
export MYCLAW_REAL_LLM_MODEL=example-model
./scripts/run_fullstack.sh
unset MYCLAW_REAL_LLM_API_KEY
```

真实模式只把 `env://MYCLAW_REAL_LLM_API_KEY` 保存为 Model Credential Ref。密钥不得写入 `.env.test`、Model Registry、测试快照、日志或 Trace。自建 CI 的真实模型 Gate 只能作为受保护的手工门禁触发，并从编排器的密文 Secret 注入 `MYCLAW_REAL_LLM_API_KEY`。

它会构建并启动以下隔离服务：

- MySQL：`localhost:3307`
- Redis：`localhost:6380`
- Mock LLM：`localhost:9090`
- API：`localhost:8001`
- Frontend：`localhost:8081`
- 两个 durable runtime worker、一个 sandbox worker
- OTel Collector（接收真实 OTLP/HTTP Span 与 AgentInvocation Link）

然后通过真实 HTTP 执行七个产品验收场景、核对关键 OpenAPI 路径，并以 Playwright Chromium 在真实前端完成 Chat、Plan、Group 与管理面验收。真实模型模式下，单个 Run 仍须在 180 秒内结算；综合场景只扩大总测试窗口，以容纳多个串行真实调用。需要同时执行快速回归和全栈验收时：

```bash
./scripts/run_all.sh
```

可覆盖 API 地址：

```bash
MYCLAW_FULLSTACK_API_URL=http://localhost:8001 ./scripts/run_fullstack.sh
```

测试栈不会自动销毁，便于失败后检查数据库和日志。完成后可执行：

```bash
docker compose -f docker-compose.test.yml --profile fullstack down -v
```

该命令会删除测试栈及测试卷，只能在 `test/` 目录、确认目标 Compose 文件无误后执行。

## 生产运行参数

根目录 `.env.example` 只列变量名和非敏感默认值。生产 Compose 强制 `MYCLAW_ENVIRONMENT=production`，会拒绝默认 `SECRET_KEY`、默认 `ENCRYPTION_KEY`、默认管理员密码、`DEBUG=true`、非正数池/租约参数，以及不短于 Runner Lease 的 Heartbeat。

Runtime 与 Sandbox 的轮询、Outbox Claim Lease、Runner Lease/Heartbeat、Sandbox Stale Window 和数据库连接池均可通过环境变量调优。启用本地 Collector 时使用：

```bash
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318 \
docker compose --profile observability up -d --scale runtime-worker=2
```

Collector 默认只输出调试 Trace；正式环境应在 `deploy/otel-collector.yaml` 中替换或增加受控后端 exporter。

生产 Frontend 使用 frozen `pnpm-lock.yaml` 构建并由 Nginx 提供静态文件，不使用 Vite preview。入口限流、Retention、备份恢复、发布与故障处置步骤见[生产运行手册](../docs/operations/phase-one-runbook.md)。
