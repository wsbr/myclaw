# `uat_single_node` 受控验收环境

该环境固定 Compose 项目 `myclaw-uat-single-node`，使用 8201/8281 等独立端口和
`myclaw-uat-single-node-*` 外部卷。它不会读取、迁移或删除旧 `myclaw-uat-*` 卷。

交付提交完成且工作区干净后执行：

```bash
deploy/uat_single_node/init-env.sh deploy/uat/.env
deploy/uat_single_node/up.sh
```

`init-env.sh` 只从旧环境读取真实模型 Key/Endpoint/Model，其他 Secret 全部重新生成，
并把 `.env`、Release 和证据目录绑定到当前 clean SHA。`up.sh` 在 SHA 不匹配或工作区
不干净时拒绝部署。真实凭证、备份和证据目录均被 Git 忽略。

默认入口为 `http://localhost:8281`，API 为 `http://localhost:8201`。只有一个 API、
两个 Runtime Worker 副本、一个 Sandbox Worker、一个 Maintenance Worker、一个 MySQL
和一个 Redis 实例；两个 Runtime 副本覆盖 lease/fencing 恢复。
这是单机 UAT，不是 Kubernetes/HA 或 enterprise production。

备份使用：

```bash
deploy/uat_single_node/backup.sh
```

真实模型、API、SSE、故障注入与 Chromium 验收使用：

```bash
deploy/uat_single_node/run-acceptance.sh
deploy/uat_single_node/observe.sh
```

`observe.sh` 默认执行要求的 30 分钟部署后观察（每 30 秒采样），记录 health、5xx/429、
Provider 失败、Outbox age、Runner lease、Checkpoint recovery、Sandbox attempt、数据库连接池
错误特征、事件序列/终态完整性和 Span Link；输出写入同一 clean SHA 证据目录。

恢复演练必须恢复到临时、全新的 MySQL 卷/数据库并校验 Alembic head、行数、归档
封印触发器和业务冒烟；不得覆盖当前 UAT 或旧 UAT 卷。

```bash
deploy/uat_single_node/backup.sh
deploy/uat_single_node/restore-drill.sh /absolute/mysql-backup.sql.gz /absolute/app-data.tar.gz
deploy/uat_single_node/deployment-drill.sh <previous-compatible-clean-sha>
```

部署/回滚演练会先备份，切到与 `0029` Schema 兼容的上一 clean SHA 镜像，执行真实
登录与 Run ORM 查询，再切回最终 SHA 并重复查询；它不会 downgrade 或覆盖数据库。
