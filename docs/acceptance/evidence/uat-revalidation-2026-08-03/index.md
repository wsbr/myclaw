# MyClaw 一期 UAT 修复复验证据索引

本目录只保存脱敏后的 UAT 证据。截图、下载校验和摘要均不包含账号密码、模型密钥、Token 或 Authorization Header。

| 编号 | 文件 | 说明 |
| --- | --- | --- |
| E01 | `02-five-agents-published.png` | 五个零售垂类 Agent 均已发布；总控、供应链、售后、财务启用 Group，营销不启用 |
| E02 | `03-chat-natural-tool-failure.png` | Chat 自然业务请求得到文字判责，但工具语法被当作普通文本、Artifact 数为 0 |
| E03 | `04-plan-four-node-succeeded.png` | 双十一 Plan 的四节点 DAG、并行专业节点及 Owner 汇总全部成功 |
| E04 | `05-group-natural-tool-failure.png` | Group 能文字说明权限边界，但没有执行持久澄清、Agent 调用或历史检索 |
| E05 | `06-version-freeze-v2-chat.png` | 总控发布 v2 后，新 Chat 返回 `UAT-V2` 版本标记 |
| E06 | `07-tool-governance.png` | `execute` 恢复启用并设为 `never` 后，无确认返回真实计算结果 1217 |
| E07 | `08-chinese-artifact-published.png` | 精确 Tool 契约下中文 Artifact 已发布并出现在下载列表（技术控制） |
| E08 | `artifact-download-check.md` | 中文 Artifact 下载响应头、SHA-256 与正文校验 |
| E09 | `event-trace-summary.md` | 关键 Run、版本冻结、Tool 治理、Runner fencing、事件与 Trace 摘要 |
| E10 | `proposal-stability.md` | 30 次真实模型 Proposal 的最终统计、重试与延迟 |

## 证据边界

- 自然业务场景与技术控制用例严格分开；带 `call <tool>` 或 `You must call <tool>` 的结果不计入产品 UAT 通过率。
- API 和数据库只用于读取 UI 未完整展示的标识符、版本、事件、Trace 和冻结证据。
- UAT 容器、持久卷、Agent、会话、Plan、Group 和 Artifact 均保留，未执行清理。
