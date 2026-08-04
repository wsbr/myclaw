# 30 次真实模型 Proposal 稳定性

本文件记录 30 个通过 Web UI 提交的轻量真实经营 Plan Proposal。每个样本要求为次日门店库存盘点生成“库存核对、异常复核、负责人汇总”的关系化计划；未使用 Mock 模型或平台 API 创建 Run。

## 暂停快照

用户要求暂停验收后，于 `2026-08-03 08:23:49` 固定只读快照；不再等待后台任务变化，也没有取消、重提或修复任何 Run。

| 指标 | 暂停时实际值 |
| --- | --- |
| 提交总数 | 30 |
| `plan.draft_ready` / `awaiting_confirmation` | 25 |
| 最终失败 | 1 |
| 尚未收口 | 4（2 running、2 queued） |
| 首轮 `invalid_structured_output` | 29 |
| 首轮因 Worker 恢复转为 `lost` | 1 |
| 自动修复后最终成功 | 24 个 epoch 2；另 1 个恢复用例 epoch 3 成功 |
| 自动修复后最终失败 | 1，`invalid structured Plan proposal after repair: JSONDecodeError` |
| `incomplete chunked read` / 零输出错误 | 0 / 0 |
| 唯一性 | 30 Run、30 Plan、30 Work Item；每个 Run 最多 1 个 `plan.created`、1 个 `plan.draft_ready` |

最终失败 Run：`019fc68e-daf5-7d12-a9b6-3c4ba011e584`。其 epoch 1 为 `invalid_structured_output`，epoch 2 自动修复后仍以 `runner_execution_failed` 终止，Plan 状态为 `cancelled`。

## 已成功样本延迟

对暂停时 25 个 draft-ready 样本，延迟按 Run 创建到 Plan 进入 `awaiting_confirmation` 计算：

- 最小：352 秒（5 分 52 秒）。
- 平均：1533.4 秒（约 25 分 33 秒）。
- P50：1579 秒（26 分 19 秒）。
- P95：2597 秒（43 分 17 秒）。
- 最大：2706 秒（45 分 06 秒）。

## 判断

- 30/30 最终成功门槛已经不可能达到，因此本项 **不通过**。
- 本批次没有观察到 `incomplete chunked read` 或零输出错误，但不能据此把稳定性整体判为通过：29 个首轮执行发生结构校验错误，且至少 1 个样本自动修复后仍最终失败。
- 端到端延迟包含持久队列等待和模型重试，是用户实际等待时间；不通过调参、扩容或重提样本美化结果。

30 个 Proposal 及暂停时的未完成任务均保留在 UAT 数据库中，未确认执行、未取消、未删除。
