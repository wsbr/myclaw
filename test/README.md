# MyClaw 平台 Tool 测试与验收

本目录只保留两类门禁：不涉及 Agent 决策的确定性合同/安全/真实数据库检查，以及调用管理员当前真实主模型、真实 Provider 和真实 Chromium 的全栈验收。不存在替代模型、替代 Search、替代 Browser、替代 MCP 或 marker 驱动的 Agent 验收路径。

## 目录

```text
test/
├── unit/backend/test_platform_tool_contracts_v3.py
├── integration/test_platform_tool_v3_mysql.py
├── e2e/fullstack/
│   ├── conftest.py
│   ├── test_platform_tool_v3_real.py
│   └── test_ui_acceptance.py
├── docker-compose.test.yml
└── scripts/
    ├── run_ci_gates.sh
    ├── run_unit.sh
    └── run_fullstack.sh
```

## 确定性门禁

该门禁不调用模型，只验证 Draft 2020-12 Schema、Provider wire 转换、AES-GCM Secret、SSRF/路径安全、空 MySQL migration、不可变触发器和前端生产构建。

```bash
MYCLAW_ACCEPTANCE_COMMIT_SHA="$(git rev-parse HEAD)" \
  ./scripts/run_ci_gates.sh standard
```

脚本只接受固定且干净的 commit。

## 真实能力验收

以下变量必须由 shell 或 CI Secret 注入；不得写入 `.env.test`、日志、Trace、JUnit 或快照：

```bash
export MYCLAW_REAL_LLM_ENDPOINT=https://provider.example/v1
export MYCLAW_REAL_LLM_MODEL=real-model-id
read -r -s MYCLAW_REAL_LLM_API_KEY && export MYCLAW_REAL_LLM_API_KEY
read -r -s MYCLAW_BOCHA_API_KEY && export MYCLAW_BOCHA_API_KEY
read -r -s MYCLAW_BRAVE_API_KEY && export MYCLAW_BRAVE_API_KEY
MYCLAW_ACCEPTANCE_COMMIT_SHA="$(git rev-parse HEAD)" \
  ./scripts/run_ci_gates.sh real
```

真实门禁构建并运行 MySQL、Redis、API、两个 Runtime Worker、Sandbox Worker、Connector Worker、Browser Worker、Frontend 和 OTel Collector。测试通过管理员 write-only API 配置真实模型、Bocha、Brave 与公开 OpenAI Developer Docs MCP，然后完成：

- Bocha/Brave 中英文与 freshness 搜索，真实 HTML/PDF Fetch；
- 30 个自然语言盲测与五类多步任务；
- Workspace→Execute→Artifact、Search→Fetch→引用、Browser 精确审批、跨 Session Memory、动态 MCP；
- Chat、Plan、Group 自然停止、批准前零副作用、批准后实施与 Worker 重启恢复；
- Playwright 真实产品 UI 的管理配置、Chat/Plan、Browser 审批、Tool 账本、引用、截图和 Artifact 下载。

测试栈端口默认为 MySQL `3307`、Redis `6380`、API `8001`、Frontend `8081`。清理仅限已核对的测试 Compose 项目：

```bash
docker compose -f docker-compose.test.yml --profile fullstack down --volumes --remove-orphans
```
