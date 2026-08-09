# 本地持久 UAT 环境

这套环境供产品在尚未部署到企业服务器前，在同一台开发电脑上持续验收。它与开发环境和 CI 测试栈隔离，固定使用 Compose 项目 `myclaw-uat`、独立端口及外部命名卷。Browser Tool 由独立 `browser-worker` 执行，每个 MyClaw Session 使用不继承宿主浏览器 Cookie/Profile 的隔离 Chromium Context。

## 初始化与启动

```bash
deploy/uat/init-env.sh
deploy/uat/up.sh
```

初始化脚本会隐藏读取真实模型 API Key，生成权限为 `0600` 的 `deploy/uat/.env`，并创建三个外部卷。该文件和备份目录均被 Git 忽略。产品访问 `http://localhost:8181`；管理员邮箱为 `admin@uat.myclaw.local`，初始密码从本机 `deploy/uat/.env` 的 `INIT_ADMIN_PASSWORD` 获取。

首次进入“模型管理”时，保存以下配置；管理 API 接收完整 Key 后立即加密，之后由 UAT 数据库持续复用密文：

- Provider：`openai_compatible`
- Endpoint：`.env` 中的 `MYCLAW_REAL_LLM_ENDPOINT`
- Model：`.env` 中的 `MYCLAW_REAL_LLM_MODEL`
- API Key：输入完整 `MYCLAW_REAL_LLM_API_KEY` 值（页面与查询 API 不回显）

数据库不保存明文 Key。新建 v2 ModelVersion 的管理页面和公开 API 都要求一次性提交完整 Key；历史记录中的 `env://` 引用仍可继续解析，但不再作为新版本输入。

## 日常操作

```bash
# 查看状态
docker compose --env-file deploy/uat/.env -f deploy/docker-compose.uat.yml ps

# 停止但保留全部数据
deploy/uat/stop.sh

# 重启或升级
deploy/uat/up.sh

# 创建带校验和的逻辑备份
deploy/uat/backup.sh
```

OTLP Trace 同时写入平台数据库的 Runner Trace/Span 模型与外部应用卷中的
`otel/uat-otel-traces.jsonl`。启动时的一次性 `otel-volume-init` 服务仅创建该专用目录，
并由与 Collector 相同的固定 `10001:10001` 身份创建；该容器使用只读根文件系统、
`no-new-privileges` 且不保留任何 Linux capability，不会递归修改应用卷内其他文件。
Collector 文件按 32 MiB 轮转，保留 30 天、最多 10 个备份；容器重建不会删除该文件。
Collector 健康端口默认为 `http://127.0.0.1:13135/`。

不要把 CI 的 `test/docker-compose.test.yml` 用作产品验收环境。CI 的 MySQL 使用 `tmpfs`，停止容器后数据即消失。UAT 的 `myclaw-uat-mysql-data`、`myclaw-uat-redis-data` 和 `myclaw-uat-app-data` 是 Compose 外部卷，即使误执行 `down -v` 也不会由 Compose 删除。

Tool v3 是一次性破坏性重构，只允许使用 `deploy/uat/destructive-reset.sh` 清空这三个精确卷。脚本会先核对所有挂载容器的 Compose project、working directory 和 config file，拒绝非本仓库 MyClaw 资源；它不会创建备份，并要求显式设置 `MYCLAW_CONFIRM_DESTRUCTIVE_RESET=ERASE_MYCLAW_UAT_WITHOUT_BACKUP`。清空后脚本运行新 Alembic head、重建 bootstrap 管理员；模型、所选 Search Provider 与 MCP 密钥必须由管理员重新通过 write-only 字段提交。

重置完成后，对同一套 `myclaw-uat` 服务执行 `deploy/uat/run-acceptance.sh`。脚本拒绝脏工作树或不匹配的固定 Commit，并要求真实主模型、显式选择的 Bailian 或 Bocha/Brave Search 模式、公共 OpenAI Docs MCP 与隔离 Chromium 全部可用；发布证据写入被 Git 忽略的 `deploy/uat/evidence/<commit>/`。

默认只绑定本机回环地址。如需让同一局域网的另一台设备访问，必须同时调整 `UAT_BIND_HOST`、`UAT_API_PUBLIC_URL` 与 `CORS_ORIGINS`，然后重建 Frontend，不能只开放端口。
