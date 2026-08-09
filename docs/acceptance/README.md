# MyClaw 验收文档

本目录维护 MyClaw 各阶段的产品验收报告、验收资产和脱敏证据。验收报告记录测试环境中的实际结果，不替代 CI、生产变更审批或正式发布证据。

> 2026-08-04以前的报告验证的是V1 Runtime和补丁Harness，不能作为[Agent-native v2第一阶段方案](../technical/phase-one-agent-native-delivery.md)的通过证据。v2必须在clean SHA和全新UAT基线上重新执行完整门禁；历史失败结论与证据继续保留。

## 一期验收

- [一期当前实现封板审计（2026-08-09）](phase-one-closure-audit-2026-08-09.md)
- [首次测试基线：一期真实业务 UAT 报告（2026-08-03）](phase-one-uat-2026-08-03.md)
- [修复后持久 UAT 复验报告（2026-08-03）](phase-one-uat-revalidation-2026-08-03.md)
- [Harness 修复实现与候选验收报告（2026-08-03）](phase-one-harness-remediation-2026-08-03.md)
- [小型软件项目开发组三模式UAT报告（2026-08-04）](phase-one-dev-team-uat-2026-08-04.md)
- [首次测试基线证据索引](evidence/index.md)
- [修复后持久 UAT 复验证据索引](evidence/uat-revalidation-2026-08-03/index.md)
- [Harness 修复候选证据索引](evidence/harness-remediation-2026-08-03/index.md)
- [小型软件项目开发组证据索引](evidence/dev-team-uat-2026-08-04/index.md)
- [零售证据分析 Skill 源文件](assets/retail-evidence-analysis/SKILL.md)

## 目录约定

- `assets/`：为验收装配的非生产资产。
- `evidence/`：脱敏截图和只包含必要标识符的证据摘要。
- 报告不得记录账号密码、模型密钥、Token 或 Authorization Header。

## 现场状态

首次测试基线使用 `8081/8001` 隔离栈；修复后复验使用 `8181/8101` 持久 UAT 栈。Harness 修复候选另在隔离全栈中完成 Mock 与真实模型门禁，并将同一工作树重建到持久 UAT 做数据、配置和观测复核。历史现场、Agent、会话、Plan、Group、Artifact 和测试数据均保留，没有清理数据卷或删除验收对象。复核完成后应轮换本次使用的模型密钥。
