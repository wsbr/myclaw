# MyClaw 验收文档

本目录维护 MyClaw 各阶段的产品验收报告、验收资产和脱敏证据。验收报告记录测试环境中的实际结果，不替代 CI、生产变更审批或正式发布证据。

## 一期验收

- [一期真实业务 UAT 报告（2026-08-03）](phase-one-uat-2026-08-03.md)
- [证据索引](evidence/index.md)
- [零售证据分析 Skill 源文件](assets/retail-evidence-analysis/SKILL.md)

## 目录约定

- `assets/`：为验收装配的非生产资产。
- `evidence/`：脱敏截图和只包含必要标识符的证据摘要。
- 报告不得记录账号密码、模型密钥、Token 或 Authorization Header。

## 现场状态

本次验收按要求保留隔离测试栈、Agent、会话、Artifact 和测试数据。缺陷修复复验期间重建了应用容器，但没有清理 MySQL 数据卷或删除验收对象。复核完成后应轮换本次使用的模型密钥。
