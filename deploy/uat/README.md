# 本地持久 UAT 环境

这套环境供产品在尚未部署到企业服务器前，在同一台开发电脑上持续验收。它与开发环境和 CI 测试栈隔离，固定使用 Compose 项目 `myclaw-uat`、独立端口及外部命名卷。

## 初始化与启动

```bash
deploy/uat/init-env.sh
deploy/uat/up.sh
```

初始化脚本会隐藏读取真实模型 API Key，生成权限为 `0600` 的 `deploy/uat/.env`，并创建三个外部卷。该文件和备份目录均被 Git 忽略。产品访问 `http://localhost:8181`；管理员邮箱为 `admin@uat.myclaw.local`，初始密码从本机 `deploy/uat/.env` 的 `INIT_ADMIN_PASSWORD` 获取。

首次进入“模型管理”时，保存以下非敏感配置；之后由 UAT 数据库持续复用：

- Provider：`openai_compatible`
- Endpoint：`.env` 中的 `MYCLAW_REAL_LLM_ENDPOINT`
- Model：`.env` 中的 `MYCLAW_REAL_LLM_MODEL`
- Credential Ref：`env://MYCLAW_REAL_LLM_API_KEY`

数据库只保存 Credential Ref，不保存真实 Key。API 与 Runtime Worker 从受限环境文件注入 Key。

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

不要把 CI 的 `test/docker-compose.test.yml` 用作产品验收环境。CI 的 MySQL 使用 `tmpfs`，停止容器后数据即消失。UAT 的 `myclaw-uat-mysql-data`、`myclaw-uat-redis-data` 和 `myclaw-uat-app-data` 是 Compose 外部卷，即使误执行 `down -v` 也不会由 Compose 删除；仍不得手工运行 `docker volume rm`。

默认只绑定本机回环地址。如需让同一局域网的另一台设备访问，必须同时调整 `UAT_BIND_HOST`、`UAT_API_PUBLIC_URL` 与 `CORS_ORIGINS`，然后重建 Frontend，不能只开放端口。
