# 第一阶段交付状态与验收证据

本文维护 MyClaw 第一阶段的实现缺口和证据契约。产品边界只来自[第一阶段交付边界](../modules/phase-one-scope.md)；本文不扩展验收标准，也不把旧 Commit 的测试结果当成当前发布证据。

## 缺口闭环清单

| 缺口 | 处置 | 验证方式 |
| --- | --- | --- |
| CI 引用已删除的旧 Integration/UI 路径 | 拆分为 Unit/关键 branch coverage、Frontend frozen build、空库 Migration、双 Runtime Worker Mock Full-stack 和手工 Secret 驱动的 Real-model Gate | `.github/workflows/test.yml`；每个 Job 上传 JUnit/coverage/诊断 Artifact |
| 旧 Task/Celery/MCP Runtime 与双模型残留 | 删除旧路由、模型、服务、Worker、前端入口和测试；Database Outbox 成为唯一可靠调度源 | 运行目录 `rg` 审计；历史迁移和明确标注的设计历史不算运行引用 |
| Migration 分叉风险 | 保留线性历史迁移并以 0021 删除旧产品表，0022 增加 OTel parent context | 空 MySQL 8 从 0001 升至 0022；`alembic heads` 必须只有一行 |
| Trace 只有数据库投影 | 增加 OTel SDK/OTLP，导出 `runner_execution`、`model_generation`，AgentInvocation 创建真实 Span Link | Docker Collector 日志 + 数据库 Trace Link 双重断言 |
| 并发领取、租约和迟到结果缺少直接证明 | 增加双 Worker claim、claim/epoch fencing、checkpoint 恢复、重复投递、取消竞态、迟到结果和 terminal-event 唯一测试 | `test_runtime_worker_recovery_v1.py` 与全栈 Worker restart 场景 |
| 并行 Plan 在 MySQL 死锁时被误判业务失败 | 统一 Runtime durable transition 锁顺序；只对 MySQL 1205/1213 做四次有界重试 | 1205/1213/非瞬态单测；双 Worker Plan 必须仍以 `succeeded` 结算 |
| 真实模型在 Plan 子 Agent 完成后重复委派直至超时 | 子 Agent batch 终态后直接结算专用协调 WorkItem 并重新扫描冻结图，不恢复 Owner 模型；普通 Chat/Group AgentInvocation 仍恢复调用方 | execution epoch 不增长、无 replacement Runner 的定向单测；真实模型 Plan 必须在时限内 `succeeded` |
| Group Formation 与 API 权限/幂等分支不足 | 增加结构错误聚合、可见性/发布状态、版本冻结、部分失败、owner unavailable、幂等 mismatch/replay 测试 | 关键模块 branch coverage Gate |
| Frontend 构建不确定且使用开发预览服务器 | 固定 pnpm 10.15.0 与 lockfile，Docker 内 `--frozen-lockfile`，Nginx 提供静态 SPA 和 `/healthz` | Frontend build Job + production image build + Docker healthcheck |
| 无入口限流 | Redis 固定窗口；认证主体/IP 先 HMAC，不保存原始值；Redis 故障显式降级而不破坏数据库权威路径 | `test_rate_limit.py`；429 header 与降级 header 行为 |
| 无 Retention/Cleanup | 单 leader、受限批次，只清理过期 operational rows，并写 `retention.cleanup` 审计 | `test_retention_service.py` 与配置下限测试 |
| 缺少备份恢复与事故处置 | 提供压缩逻辑备份、SHA-256 验证、显式恢复确认和故障矩阵 | [生产运行手册](../operations/phase-one-runbook.md) 与 `deploy/scripts/` |
| 真实模型结果和七场景证据分散 | 一个 API 综合测试按七场景逐项记录 JUnit suite property，另有 Chromium UI 测试 | `test_phase_one_acceptance.py`、`test_ui_acceptance.py`、正式 evidence manifest |
| Group/Plan 视图无法处理模型临时请求的 Tool Confirmation/Clarification | required action 卡片改为三种会话模式共用；Chromium 通过页面按钮批准或填写回答，并继续严格等待 GroupRun 完成 | 真实模型浏览器测试的 required-action POST 必须成功、次数有界，最终完成断言仍保留 |
| 真实模型偶发返回不可解析的 Plan/Group 结构化提案 | OpenAI-compatible streaming 传递冻结的 temperature/max_tokens；严格校验失败时使用 savepoint 回滚，仅排队一次 fenced repair Runner，不保存原始无效输出；第二次无效仍失败 | 模型参数转发、单次 epoch/attempt 递增、第二次拒绝重试单测；真实 API 与 Chromium 仍要求有效图并完成 |

## 七个场景映射

| # | 必须保持的成功断言 |
| --- | --- |
| 1 | Builder 选择 Model、Tool、Skill、四类 Policy、Local Sandbox、绑定 Agent 和 `group_enabled`，发布不可变版本，并通过 Agent API Key 调用和撤销 |
| 2 | Chat 完成 SSE model delta + reliable event、Tool Confirmation、durable async Tool、断线重连、文件写入、Artifact 发布与逐字下载；Worker/Sandbox 重启不丢状态 |
| 3 | Plan 提案可编辑并冻结，三节点由双 Worker 并行执行，Owner 汇总且综合场景必须 `succeeded`；单元层另验证部分失败结算 |
| 4 | 三 Agent Group 完成 Formation、定向 Clarification、二次 AgentInvocation、共享历史显式查询，并拒绝图外 Agent |
| 5 | 新 Chat 使用新发布版本；已确认 Plan 和既有 GroupRun 的参与版本保持冻结 |
| 6 | Tool 停用使尚未 dispatch 的调用安全失败；重新启用并改 Confirmation Policy 后只影响新调用 |
| 7 | Outbox/Runner active 阶段重启双 Worker，只保留一个 WorkItem、两代 Runner（lost/succeeded，epoch 1/2）和唯一 `run.completed`；Collector 同时收到 generation span 与 invocation link span |

## 覆盖率与 CI Gate

CI 对 Runtime Worker、Group Formation、Agents API、Groups API 启用 branch coverage，综合下限为 30%。本轮预提交基线为 105 项单测全部通过：Runtime Worker 40%、Group Formation 100%、Agents API 42%、Groups API 30%，综合 40.42%。覆盖率只用于防回退；场景断言、错误语义和真实模型 Gate 不得因覆盖率而放宽。

## 正式证据契约

正式发布必须在固定且无 tracked 修改的 Commit SHA 上重新运行全部门禁。JUnit 和 coverage 写入 `test/test-results/`，该目录不提交并由 CI 作为 Artifact 上传。`test/scripts/collect_acceptance_evidence.py` 从报告生成脱敏 JSON manifest，至少包含：

- Commit SHA、UTC 生成时间、tracked worktree 状态；
- 测试命令、JUnit 用例数/失败数/耗时和报告 SHA-256；
- Endpoint、Model、七场景与 Chromium 结果；
- Frontend 和空库 Migration 结果、关键 branch coverage；
- CI Run 链接；
- 固定说明“Credential 只由环境/Secret 注入，值未采集”。

没有 CI Run 链接、真实模型 JUnit、七场景 property 或者 `tracked_worktree_dirty=false` 的 manifest 不能作为正式交付证据。失败日志可以保存，但必须先做凭证模式扫描；禁止保存 API Key、Authorization header、Prompt 中的 Secret 或数据库明文。

## 保留风险

- 真实模型延迟与输出受外部服务波动影响；Mock 只用于快速回归，不得替代发布 Gate，也不允许静默 fallback。
- 前端主 bundle 仍约 1.55 MB，有分包警告但不影响第一阶段正确性；后续应做性能优化。
- 第一阶段生产拓扑是单机 Compose，Local Sandbox 不是不可信代码强隔离；Kubernetes HA、Remote/AIO Sandbox、独立 Artifact Store 均在权威边界中明确后置。
- 逻辑备份脚本和 Runbook 已交付，但每个部署环境仍必须实际完成隔离恢复演练，未演练的备份不能视为可恢复证明。
