# MyClaw 一期 Agent Harness 修复实现与候选验收报告

## 1. 结论

本轮已完成第一批 Harness 修复、真实垂类 Agent 装配、三模式自然语言端到端验证，以及持久 UAT 的备份、重建、模型能力校准和 Trace 落盘验证。

| 结论维度 | 状态 | 说明 |
| --- | --- | --- |
| 确定性工程门禁 | **通过** | 133 个单元测试、Mock 全栈 API/UI、故障注入、单头 Migration 均通过。Tool 权限不再由用户文本决定，副作用必须由权威记录证明。 |
| 真实模型候选门禁 | **通过** | `qwen3.5-plus` 下 Chat、Plan、Group 七场景综合验收通过；真实模型报告 3/3 通过。 |
| 持久 UAT 部署复核 | **通过** | 数据备份与校验通过；重建前后业务计数一致；模型能力版本为 v2；服务健康；真实 OTLP span 已持久化。 |
| 历史 UAT 结论 | **保持不通过** | 原始失败 Run、AgentVersion 和证据没有改写或删除。本报告是修复候选的新证据，不倒推修改历史结论。 |
| 正式发布门禁 | **仍阻断** | 尚未在固定 Commit SHA 的正式 CI 上完成同一候选的可访问归档，也未完成计划要求的真实模型 Proposal 30/30 统计门禁。 |

因此，本轮可以交付为“第一批 Harness 修复候选”，不能标记为“完整 Harness V2 已完成”或“正式生产发布通过”。

## 2. 根因判断

用户关于“AICoding 交付补丁导致全面工程性塌陷”的判断方向基本成立，但更精确的结论是：补丁链是直接触发因素，缺失的协议、状态和测试护栏让局部补丁扩散成三模式系统性失效。

历史 UAT 持久库在本轮重建前后均保留以下事实：

| 指标 | 现场值 |
| --- | ---: |
| Run | 53 |
| Runner | 110 |
| ExecutionEvent | 535 |
| Runner Trace | 110 |
| Observability Span | 223 |
| `invalid_structured_output` Runner | 31 |
| `runner_execution_failed` Runner | 3 |
| lost Runner | 1 |

31 个结构失败中，Plan 占 30 个，Group 占 1 个。Plan 的 31 个 Run 中 29 个仍在等待、1 个成功、1 个失败；这与原 UAT 报告中 Proposal 大面积首轮解析失败相互印证。

故障链为：

1. 补丁把 Tool 暴露错误绑定到用户文本是否包含内部 Tool key，普通中文业务表达得不到已经装配的能力；
2. Plan/Group 仅靠 Prompt 要求 JSON，没有向 Provider 声明响应契约，结构失败被当作常规概率波动；
3. 模型文本、Tool Call、已提交副作用和最终成功之间没有统一完成合同，造成“文字声称做了、平台实际没做”；
4. 恢复 Runner 没有完整保留 Group 拓扑、输入和主体快照，批准、异步回调或重试后可能发生能力漂移；
5. 测试输入使用内部 Tool 口令，验证了补丁路径，却没有验证产品自然语言；
6. Runner Snapshot 记录装配 Tool，不记录最终暴露 Tool；Collector 又只有 debug 输出，导致“配置存在”被误判为“能力可用、Trace 可归集”。

这不是单一 Prompt、单一模型或单一正则的问题；根本问题是权限、模型协议、状态机、效果证明和发布门禁没有形成一个闭环。

## 3. 本轮修复范围

### 3.1 机器态 Capability Envelope

- 删除用户/任务文本对 Tool 授权的影响；
- Tool 集合改为 AgentVersion 装配、执行主体、模式、阶段、冻结 Plan/Group 图和当前平台治理的交集；
- Chat/Plan 的被委派节点不能继续扇出，Group 只保留冻结图内允许的协作能力；
- 已回答的 Clarification 不再向后续 Runner 暴露；
- Dispatcher 再次拒绝未实际暴露、未装配、图外或已停用 Tool；
- Trace 同时记录 `assembled_tool_keys`、`effective_tool_keys` 和 `harness_tool_keys`，不再用装配事实冒充 Provider 实际能力。

### 3.2 Response Contract

- Plan Proposal 使用 `structured.plan_proposal.v1`；
- Group Formation 使用 `structured.group_formation.v1`；
- 根据模型注册能力选择 `json_schema` 或 `json_object` wire contract；
- OpenAI-compatible Adapter 透传 `response_format`，仅在尚未产生任何 payload 时对瞬时传输错误重试一次；
- 结构失败只允许一次有边界的 repair，不进行无限排队；
- Registry 拒绝未知 `structured_output` 能力值。

### 3.3 Completion Contract 与 Effect Ledger

新增 Harness 控制 Tool `finalize_response`。它不是业务授权，也不进入 Platform Tool Registry；它只允许模型提交封闭的完成信封：

```text
message
required_tool_keys[]
reason_code
```

可声明效果被限制在已知业务 Tool 集合。Harness 从 `ToolInvocation`、`AgentInvocation`、`Clarification`、Workspace 写入、Artifact 和 Group 历史查询等权威记录构建 Effect Ledger，再决定是否允许结算。

- 必需效果未发生：不允许 plain text 成功，只安排一次修复；
- 已完成的一次性效果再次被模型调用：拒绝重复执行，并安排一次 finalization repair；
- 模型以严格完成信封声明唯一 `request_clarification`：在该 Tool 确实已暴露时，Harness 可安全物化持久 Clarification；
- Provider 在效果全部真实完成后偶发退化为普通文本：只在 Ledger 非空、没有 pending/unavailable、且文件发布链完整时接受；
- 没有任何 Ledger 的纯文本首轮、仍有 pending 效果或缺失能力时，fallback 一律拒绝；
- `execute`、`write_file`、`publish_artifact`、`request_clarification`、`search_group_history` 等一次性动作不会因模型重试而重复执行。

真实 Provider 验证还发现 `qwen3.5-plus` 的 thinking 路径拒绝 `tool_choice=required`。实现没有伪造兼容性，而是保留 Tool 定义、采用封闭完成信封和服务端效果校验；结构化 Proposal 继续使用该 Provider 支持的 `json_object`。

### 3.4 等待、恢复与可观测性

- Tool 审批和显式 retry 恢复时保留 Group 拓扑、输入与 ExecutionPrincipal 快照；
- Runner 诊断返回完成合同、repair 次数、满足/待满足效果以及最终 settlement mode；
- Mock Provider 注入无效完成信封、重复隐藏副作用和最终 plain text，验证有界修复与安全 fallback；
- UAT Collector 增加持久 file exporter，并由一次性最小权限 init 服务创建专用目录。

## 4. 真实垂类 Agent 装配

端到端验收使用“电商退款审计”业务域，而不是通用助手或内部 Tool 指令。

| Agent | 业务职责 | 最小 Tool 装配 |
| --- | --- | --- |
| 退款审计负责人 | 统筹运营、风控与审计交付 | 七项业务 Tool；仅入口协调者拥有完整能力 |
| 退款运营专员 | 批次、渠道、时效和口径核对 | 仅 `request_clarification` |
| 订单风控复核员 | 重复退款、账号/设备关联风险 | 无 Tool |
| 外部供应商对账员 | 图外边界测试对象 | 无 Tool |

四个 Agent 均固定真实模型、Sandbox、Context/History/Memory Policy 和不可变已发布版本。Skill 只提供退款审计证据口径，不增加任何 Tool 权限。

这套装配刻意让“领域能力”和“系统权限”分离：专员可以做领域判断，但不能因 Instructions 或 Skill 文字获得文件、执行、历史检索或委派能力。

## 5. 三模式自然语言验收

### 5.1 Chat

自然场景包含：退款申请 4087 笔、有效退款 4086 笔，要求在隔离环境验证差额、返回真实标准输出；随后要求创建并发布中文文件名的可下载审计报告。

验证结果：

- `execute` 进入真实确认卡，重启 Runtime/Sandbox Worker 后同一调用恢复；
- SSE 断线重连仍可收到 model delta 和可靠 `run.completed`；
- 一次性 `execute` 完成后从后续 effective tools 移除，但 Ledger 保留 satisfied 证据；
- `write_file` 和 `publish_artifact` 分别确认、执行，Artifact 内容和 RFC 5987 中文下载文件名均正确；
- Tool 停用时已排队调用安全失败且不 dispatch；恢复并改为 `never` 后新调用立即按新治理生效。

### 5.2 Plan

自然目标是复盘 2026 年第二季度退款异常，分别核对审计口径、退款运营和订单风控，最后形成管理层摘要。

验证结果：

- Proposal Runner 明确使用 `structured.plan_proposal.v1`；
- 用户编辑后冻结三个真实业务节点；
- 三个节点使用各自固定 AgentVersion，全部成功；
- 结算不依赖模型反向修改图；
- 后续发布新 AgentVersion 不改变已冻结 Plan 的版本映射。

### 5.3 Group

自然目标是由退款审计负责人、退款运营专员和订单风控复核员联合复核异常退款。

验证结果：

- Formation Runner 明确使用 `structured.group_formation.v1`；
- “第二季度还是六月单月”产生真实定向 Clarification，回答只恢复提出问题的节点；
- “让订单风控独立复核”产生真实 `AgentInvocation` 和跨 Runner Span Link；
- “从本组已确认讨论找出口径结论”只执行一次 `search_group_history`，Ledger 记录 satisfied；
- Mock 故障注入让模型再次请求已隐藏的历史检索，Harness 拒绝重复副作用，单次修复后只在 Ledger 已满足时接受普通文本；
- 新发布版本不改写已冻结 GroupRun，后续新 GroupRun 使用新版本。

所有产品场景均使用自然中文话术；内部 Tool key 只出现在断言、协议故障注入和安全边界验证中，不进入产品输入。

## 6. 测试证据

| Gate | 结果 | 耗时 | 证据 |
| --- | ---: | ---: | --- |
| Unit | 133/133 | 8.341 s | [unit-final.xml](evidence/harness-remediation-2026-08-03/unit-final.xml) |
| Mock MySQL/Redis/API/Worker/Sandbox/UI | 4/4 | 45.763 s | [mock-fullstack-final.xml](evidence/harness-remediation-2026-08-03/mock-fullstack-final.xml) |
| 真实模型 `qwen3.5-plus` | 3/3 | 697.270 s | [real-model-final.xml](evidence/harness-remediation-2026-08-03/real-model-final.xml) |
| Chromium UI | 通过 | 包含于 Mock Gate | Mock 报告属性 `chromium_ui_chat_plan_group_admin=passed` |
| Migration | 通过 | — | 空测试栈升级至 `0022 (head)`，只有一个 Alembic head |

真实模型 Gate 覆盖七个一期场景属性；Chromium UI 使用 Mock Provider 验证确定性交互。为避免把外部模型概率和浏览器交互混在同一失败域，本轮没有在真实 Provider Gate 中重复运行 UI 用例。

报告 SHA-256 见[证据索引](evidence/harness-remediation-2026-08-03/index.md)。所有报告当前标记 `commit_sha=uncommitted`，因此只能作为本机候选证据。

## 7. 持久 UAT 部署复核

### 7.1 数据保护与重建

- 重建前创建逻辑备份 `myclaw-uat-20260803T170933Z.sql.gz`；
- `gzip -t`、SHA-256 校验均通过；
- 备份 SHA-256：`0dd2f0f9becacb5d0232a2f04cfa61ba310e780ec3e4aa941754f8cd2799f570`；
- 使用外部 MySQL、Redis、App 三个命名卷重建；
- 重建前后 Agent=5、Run=53、Runner=110、Event=535、Span=223，Migration 均为 0022。

### 7.2 模型与 Agent

UAT 模型 `uat-primary-qwen` 保持原 Provider、Endpoint、Credential Ref 和参数不变，只校准能力声明并写入平台审计：

```json
{
  "streaming": true,
  "tool_calling": true,
  "usage_in_stream": true,
  "structured_output": "json_object",
  "native_tool_calling": true
}
```

模型配置版本为 2，参数版本仍为 1；审计动作 `model.capabilities_reconciled` 成功。后台只读核验确认 UI 显示相同能力和五个已发布业务 Agent。

五个存量 UAT AgentVersion 仍装配 6—7 个 Tool，明显宽于最小权限原则。本轮没有直接改写不可变已发布版本或替用户改变业务职责。新 Harness 会按模式、阶段和图再次收窄 effective tools，但存量装配过宽仍列为发布前 P1 配置治理项。

### 7.3 Trace 持久化

首次 OTLP 烟测发现 Collector 以 `10001:10001` 运行，而 App 外部卷根目录为 `root:root 0755`，file exporter 实际报 `permission denied`。配置存在但数据不可写，这验证了不能只做静态配置验收。

修复后：

- `otel-volume-init` 仅创建 `/var/lib/myclaw/otel`，权限 `0750`、属主 `10001:10001`；
- init 容器使用只读根文件系统、`no-new-privileges` 且只保留 `CHOWN/FOWNER`，重复启动仍幂等成功；
- Collector 写入 `/var/lib/myclaw/otel/uat-otel-traces.jsonl`；
- 文件权限为 `0640`，属主 `10001:10001`；
- 服务名 `myclaw-uat-collector-smoke`、Span `uat.collector.persistence.smoke` 与重启后的 `restart-smoke` 已真实落盘；
- Collector 健康端口返回 ready，重建后持久文件仍位于外部 App 卷。

## 8. 未关闭项与发布条件

| 优先级 | 未关闭项 | 关闭条件 |
| --- | --- | --- |
| P1 | 存量 UAT AgentVersion 装配过宽 | 由业务 Owner 在 Builder 中创建新版本，按职责最小授权，并用新版本重跑三模式 |
| P1 | 正式统计门禁未完成 | 固定模型/区域/并发/SHA，Plan Proposal 30/30、Group Formation 10/10，并报告 repair rate 与延迟 |
| P1 | 正式 CI 证据缺失 | 在自建 CI/CD 对同一 clean SHA 运行 Unit、Mock、真实模型、Migration，归档可访问 JUnit 与部署审批 |
| P2 | 完整 Harness V2 的 ModelTurn/ProviderAttempt 尚未关系化 | 完成新增表、attempt 关联、预算与 Dashboard，不能只依赖 Runner manifest |
| P2 | 执行 lane 与背压仍共用 Worker | 完成 interactive/proposal/tool-resume/settlement lane 隔离和容量基线 |
| P2 | UAT 真实业务回归尚未在本轮重新写入持久现场 | 使用本报告第 5 节相同自然话术，在新最小权限 AgentVersion 上完成并保存 UI/API 证据 |

只有上述 P1 全部关闭，且工作树固定为 clean Commit SHA 后，才能把本报告的“候选通过”升级为正式发布通过。

## 9. 导航

- [三模式 Agent Harness 稳定性优化方案](../technical/agent-harness-stability-plan.md)
- [修复候选证据索引](evidence/harness-remediation-2026-08-03/index.md)
- [历史 UAT 修复复验报告](phase-one-uat-revalidation-2026-08-03.md)
- [本地持久 UAT 运维说明](../../deploy/uat/README.md)
