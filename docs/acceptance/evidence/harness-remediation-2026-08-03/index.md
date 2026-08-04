# Harness 修复候选证据索引

本目录保存本轮最终通过的三份 JUnit 报告。中间失败报告只保留在本机 `test/results/` 供调试，不作为候选通过证据。

| 证据 | 结果 | SHA-256 |
| --- | --- | --- |
| [unit-final.xml](unit-final.xml) | 133/133 passed | `469d52f034beb9e6bd144d9ce42adc6da80ac95ea25a4c4a45356d0b20fdb425` |
| [mock-fullstack-final.xml](mock-fullstack-final.xml) | 4/4 passed | `781540eb84f9eab6d8d6cbe2dcbe06ab3fa470fb9ab6306a32bdae0a242e6e8e` |
| [real-model-final.xml](real-model-final.xml) | 3/3 passed | `30871901ad126a8a2010f7312c532704144b8689e992d810f363f298b90c538d` |

## UAT 现场校验摘要

- 逻辑备份：`deploy/uat/backups/myclaw-uat-20260803T170933Z.sql.gz`；SHA-256 `0dd2f0f9becacb5d0232a2f04cfa61ba310e780ec3e4aa941754f8cd2799f570`；压缩流与校验和均通过。
- 重建前后：Agent 5、Run 53、Runner 110、ExecutionEvent 535、ObservabilitySpan 223、Alembic `0022 (head)`。
- 模型：`uat-primary-qwen` / `qwen3.5-plus`，config version 2，parameter version 1，结构化能力为 `json_object`。
- Collector：健康；真实 smoke span 已写入外部 App 卷的 `otel/uat-otel-traces.jsonl`，文件属主 `10001:10001`、权限 `0640`。
- 敏感信息：报告不包含登录密码、模型密钥、Credential 明文、Token 或 Authorization Header。

## 证据边界

- 三份 JUnit 的 `commit_sha` 均为 `uncommitted`，属于本机候选证据，不是正式 CI 证据。
- 真实模型报告验证 API/Worker/DB/Sandbox/OTel 的七场景；Chromium UI 由 Mock 全栈报告验证。
- 历史 UAT 失败对象和截图继续由相邻的 `uat-revalidation-2026-08-03/` 目录保存，本目录不覆盖原结论。
- 本轮只读 UI 核验了 UAT 模型能力和已发布 Agent 列表；没有直接改写存量不可变 AgentVersion。
