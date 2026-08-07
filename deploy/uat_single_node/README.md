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
一个 Runtime Worker、一个 Sandbox Worker、一个 Maintenance Worker、一个 MySQL 和
一个 Redis 实例；这是单机 UAT，不是 Kubernetes/HA 或 enterprise production。

备份使用：

```bash
deploy/uat_single_node/backup.sh
```

恢复演练必须恢复到临时、全新的 MySQL 卷/数据库并校验 Alembic head、行数、归档
封印触发器和业务冒烟；不得覆盖当前 UAT 或旧 UAT 卷。
