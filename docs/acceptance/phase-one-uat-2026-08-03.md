# MyClaw 一期真实业务 UAT 报告

## 1. 结论

| 结论维度 | 状态 | 说明 |
| --- | --- | --- |
| 产品 UAT 结论 | **条件通过（首次基线）** | Chat、Plan、Group 三种核心模式均形成真实业务闭环；版本冻结、Tool 治理、Worker 恢复和可观测性得到验证。首次现场发现的缺陷及后续关闭状态见第 7 节。 |
| 正式发布门禁 | **仍阻断** | 两个 P1 已由后续修复关闭，但本次仍是本地隔离栈 UAT，没有自建 CI Run/Artifact 链接，不能作为正式发布证据。 |

> 修复说明：上表保留首次 UAT 在基线 `d1434ef` 上的原始结论。后续修复已关闭
> UAT-DEF-001、002、004、005 的代码缺陷，并补齐 UAT-DEF-003 对应传输错误的受限重试；
> 修复后的发布判定以固定 Commit 的自动化报告和本节缺陷状态为准，不能反向改写首次现场结果。

本结论适用基线为 2026-08-03 的本地隔离测试栈。测试环境、容器、Agent、会话、Artifact 和数据均按要求保留。

## 2. 验收范围与基线

- 业务域：零售电商经营。
- 仓库：`/Users/wsbr/Desktop/project/myclaw`。
- Git 分支：`vibe_1`。
- 基线提交：`d1434ef`（`runtime: enforce explicit tool capabilities`）。
- UI：`http://localhost:8081`。
- API：`http://localhost:8001`。
- Runtime Worker：2 个实例。
- 执行方式：Web UI 为主；API、数据库只读取 UI 未完整展示的版本、事件和 Trace 证据。
- 代码变更：未修改平台代码；仅新增本验收文档、证据和验收 Skill 源文件。
- 保留策略：未停止容器，未清理数据卷，未删除任何验收对象。

验收结束时 API、MySQL、Redis、Mock LLM 均为 `running/healthy`；两个 Runtime Worker 和 Sandbox Worker 为 `running`。前端页面可返回 HTTP 200，但容器健康检查为 `unhealthy`，见 UAT-DEF-005。

## 3. 环境与对象配置

### 3.1 模型

| 项目 | 实际值 |
| --- | --- |
| Model ID | `019fc574-1332-7557-9c83-6b56f7566f55` |
| Registry Key | `uat-qwen35-plus-20260803` |
| Provider | `openai_compatible` |
| Model | `qwen3.5-plus` |
| 参数 | `temperature: 0`，`max_tokens: 4096` |
| 能力 | streaming、tool calling |
| Credential | 注册表仅保存 `env://MYCLAW_REAL_LLM_API_KEY` |
| 连接测试 | 成功，UI 显示 35 ms |

模型密钥只注入测试栈运行环境，未写入本报告、截图或注册表明文。

### 3.2 Skill

| 项目 | 实际值 |
| --- | --- |
| 名称 | `retail-evidence-analysis` |
| Skill ID | `019fc575-ab33-7a4d-ac98-e2243ecb0e89` |
| Version ID | `019fc575-ab33-7b37-847d-27b91d7fdffd` |
| 状态 | published v1 |
| SHA-256 | `28f6392d1ca5ebafa3770065516c362b8ee4883e11198f79708b859352377613` |

Skill 定义证据分级、售后判责、经营指标口径、事实/推断分离和风险输出格式。Runner 快照中的 checksum 与发布版本一致。

### 3.3 Agent

| Agent | ID | 发布版本 | 可见性 | Group |
| --- | --- | --- | --- | --- |
| 零售经营总控 | `019fc581-7096-7e1e-95c0-88baaeaeeef3` | v3 | enterprise | enabled |
| 售后判责专员 | `019fc57b-2169-7f5f-a4c5-77db57ab98d9` | v1 | enterprise | enabled |
| 供应链履约专家 | `019fc57e-b2df-7eeb-b228-30dec826e3c8` | v1 | enterprise | enabled |
| 财务风控专家 | `019fc57f-906f-7095-a01e-36711e8bb019` | v1 | enterprise | enabled |
| 营销策略专家 | `019fc580-6b8a-7899-9b18-7806d362ef90` | v1 | enterprise | disabled |

总控绑定其余四个 Agent。所有 Agent 使用 `local` Sandbox、相同模型和 Skill，并装配 `read_file`、`write_file`、`execute`、`publish_artifact`、`request_clarification`、`invoke_agent`、`search_group_history`。策略为：

- `context.standard`
- `history.summary_recent`
- `memory_write.user_agent_facts`
- `memory_recall.user_combined`

Instructions 均禁止编造业务数据、伪造 Tool 结果或泄露凭证。

## 4. 验收结果总表

| 场景 | 结果 | 关键证据 |
| --- | --- | --- |
| 模型连接、Skill 和五 Agent 装配 | 通过 | E01-E04；模型连接成功、Skill checksum 固定、五 Agent 发布 |
| 草稿测试 | 失败 | 已装配 Skill 的草稿快照触发不可变约束，见 UAT-DEF-001 |
| 发布不可变版本 | 通过 | 五 Agent v1；总控后续发布 v2、v3，历史版本均可读 |
| 停用/启用 | 通过 | 营销 Agent 停用后“对话”入口禁用，重新启用后恢复；Audit 有 `agent.deactivated`、`agent.reactivated` |
| Chat：高价值订单售后判责 | 条件通过 | 澄清、文件读取、计算、写文件、发布 Artifact 全部成功；Artifact 下载失败，见 UAT-DEF-002 |
| Plan：双十一大促方案 | 通过 | E06-E07；首次流中断，重试后四节点 DAG 全成功 |
| Group：取消率异常处置 | 通过 | E08-E09、E12；首轮、定向澄清、内部调用、历史检索、图外拒绝均有事件 |
| 版本冻结 | 通过 | 老 Plan/Group 固定总控 v2；新 Chat 使用 v3 并返回 `[UAT-V3]` |
| Tool 治理 | 通过 | 停用后批准无 dispatch；`never` 下无确认成功；最终恢复 `always` |
| Worker 恢复与 fencing | 通过 | epoch 1 Runner `lost`，epoch 2 接管并成功；同一 Run 仅一个终态事件 |
| Audit、Event、Trace、Invocation Link | 通过 | 74+ 审计事件；ExecutionEvent 完整；Trace 成功；内部/图外 Invocation 均可追踪 |
| 凭证泄露检查 | 通过 | 容器日志与 ExecutionEvent 中密钥模式、Authorization Header 命中均为 0 |

## 5. 场景明细

### 5.1 装配、发布与生命周期

模型连接测试成功后才开始业务验收。Skill 上传、发布、同步成功，五个企业可见 Agent 均发布；营销 Agent 明确不启用 Group。

营销 Agent 的停用/启用通过 UI 执行：停用后列表状态为 `inactive` 且正式“对话”入口禁用；重新启用后恢复 `published`。Platform Audit 同时记录两个生命周期动作。

草稿测试未能创建，后端在复制 Skill 装配到 sealed 草稿测试快照时返回 `sealed agent assembly is immutable`。正式发布和正式 Chat 不受影响，但该能力不满足一期验收要求。

### 5.2 Chat：订单 R20260803-1042

- Session：`019fc583-f6f9-70f3-95eb-45b8649c2b44`。
- Run：`019fc583-f73a-7cc5-9ab2-cc079e1c8ccd`。
- 输入事实：实付 1199 元、签收次日申诉、屏幕破损、最初缺少开箱视频信息、退运费 18 元。
- 澄清回答：存在连续开箱视频；封签完整，外箱完好，第一次点亮前可见裂屏，无剪辑。
- 结论：支持全额退款或换货，商家承担退运费；退款金额为 `1199 + 18 = 1217` 元。

Event sequence 证明执行顺序：

1. `clarification.required`（8）→ `clarification.answered`（11）。
2. `read_file` succeeded（15）。
3. `execute` confirmation required（17）→ approved（19）→ dispatched（22）→ succeeded（25）。
4. `write_file` confirmation required（29）→ approved（31）→ succeeded（34）。
5. `publish_artifact` confirmation required（36）→ approved（38）→ `artifact.published`（41）→ Tool succeeded（42）。
6. `run.completed`（44）。

页面在澄清等待期间刷新后恢复；`execute` 等待批准期间重启 Sandbox Worker，原 Run 仍继续并成功。最终 Artifact `019fc586-1ac3-7464-9da4-7d6b82973911` 状态为 `published`，名称为 `售后判责报告-R20260803-1042.md`，大小 3889 字节。中文文件名下载返回 HTTP 500，故本场景判为条件通过。

最后一个 Runner Trace 为 `af7b42f333912fa23f4f459426e0b989`：`model_generation` 和 `runner_execution` 均为 `ok`，无 error code。

### 5.3 Plan：双十一智能家电大促

- Plan：`019fc58a-f6ad-722d-bc80-61136514edb7`。
- Run：`019fc58a-f6ac-73c9-8c39-8ae7afa8b818`。
- Graph revision：2。
- Frozen hash：`c3fda8a7812fbb93fd46f24a560660b2d038d4078e632639d8907f13a9a62f1b`。

第一次 Proposal 生成在 29.5 秒处失败，Runner error detail 为 `peer closed connection without sending complete message body (incomplete chunked read)`。受控重试成功后，通过 UI 编辑并保存 revision 2：

- `supply`：供应链容量。
- `service`：售后保障。
- `finance`：财务测算。
- `owner`：最终经营决策。
- `supply/service/finance` 三节点并行，均以 `success` 依赖汇入 `owner`。

四个业务 Work Item 均 `succeeded`。Owner 汇总明确给出库存 3000 件、预算 50 万元、毛利率不低于 18%、48 小时发货、退款率不高于 5% 的闸门、责任人和验收标准；Run outcome 为 `succeeded`。

### 5.4 Group：订单取消率异常联合处置

- Group Session：`019fc592-f2e0-79fd-b364-aa3a36ceaeff`。
- Formation revision：2。
- Content hash：`57c39cb5d20cde1d6d8fe93c7477484444c3988e73303907d4082d7477ae1644`。
- 固定成员：总控、供应链、售后、财务；营销 Agent 不在图内。

首个 GroupRun `019fc595-62b4-7b44-8a14-dd4d73c6ce8e` outcome 为 `succeeded`，四方均产出初诊。总控汇总包含取消率 3%→11%、库存延迟 20 分钟、47 笔重复扣款、客服压力、止损阈值、责任人和复盘项。

定向与工具结果：

- 供应链 `request_clarification`：进入 waiting 并生成持久 Required Action。回答入口显示在同一 Session 的 Chat 视图；通过 UI 回答“华东仓”后恢复，输出华东仓处置方案。
- 总控 `invoke_agent` → 财务：`agent_invocation.succeeded`（sequence 39），财务复核建议对 47 笔重复扣款按 100% 计提准备金，并如实列出金额、凭证和支付数据缺口。
- 总控 `search_group_history`：Tool 调用 succeeded，扫描 58 条记录，但查询“华东仓 取消率”返回 0 hits；Agent 改为引用 Bootstrap 事实，见 UAT-DEF-004。
- 图外营销调用：加强调用契约后，Runtime 产生 `agent_invocation.rejected`（sequence 79，error `agent_invocation_rejected`）。该 GroupRun 因拒绝结算为 `group_settlement_failed`，但 Group Session 状态仍为 `open`，其他 GroupRun 不受影响。

Group 页面允许继续创建独立 GroupRun；初始 Run、澄清 Run、历史检索 Run 和图外边界 Run 都保留在历史中。实际财务协作边和图外拒绝事件均可通过 ExecutionEvent 复核。

验收限制：Formation revision 2 的供应链职责文本误写入“华东仓”，导致该仓库信息在正式澄清回答前已进入固定 Group Context。本次仍有效验证了 `request_clarification` 的等待、持久 Required Action、UI 回答和同一 Work Item 恢复机制，但“全新业务事实仅通过澄清注入”的语义应在修复缺陷后的回归中使用无污染 Formation 重新验证。

## 6. 横切能力

### 6.1 版本冻结

总控版本：

- v1：`019fc581-7a39-7f56-a794-6c45b496645c`
- v2：`019fc582-8e0b-7060-919a-2e6517aa46ec`
- v3：`019fc5a1-fc8e-7856-b60b-bb141e589f7d`

Plan 和首个 GroupRun 在冻结时均固定总控 v2。发布带 `[UAT-V3]` 标记的 v3 后，新 Chat Run `019fc5a2-7df9-7bdd-9832-fbcd5a6838d5` 的 Runner 使用 v3，并返回 `[UAT-V3]`；旧 Plan/Group Runner 映射仍为 v2，无版本漂移。

### 6.2 Tool 治理

1. 创建 `execute` 待确认调用，精确参数为 Python 输出 42。
2. 在 Tool 管理 UI 停用 `execute`。
3. 返回 Chat 批准：Run `019fc5a5-8780-71f5-868c-d83579c364c0` 安全失败；事件只有 `tool.created`、`tool.confirmation_required`、`tool_confirmation.approved`，没有 `tool.dispatched`。
4. 重新启用并将确认策略设为 `never`。
5. 新 Run `019fc5a9-534b-7ca5-8473-602a630e4d05` 无确认直接 dispatch，输出 81，outcome 为 `succeeded`。
6. 验收结束前将 `execute` 恢复为 enabled + `always`，最终 config version 为 5。

### 6.3 Worker 恢复与 fencing

- Run：`019fc5ab-0b2f-7817-9dca-ba022ba78d55`。
- 原 Runner：`019fc5ab-0b2f-79d1-9922-221bbac440b5`，epoch 1，重启对应 Worker 后状态为 `lost`。
- 恢复 Runner：`019fc5ab-e680-793e-8007-8b7dd000f473`，epoch 2，同一 AgentVersion、同一 Run/Work Item，最终成功。
- Run 最终 outcome：`succeeded`。
- `run.completed` 仅一个，sequence 41；未发现重复终态事件。

该证据同时证明旧执行权被 fencing，新 Worker 能从持久状态恢复。

### 6.4 可观测性与安全

- Platform Audit 可见模型创建、Skill 创建/发布、Agent 发布、Tool 配置变更、Agent 停用/启用等动作。
- ExecutionEvent 覆盖澄清、确认、dispatch、Artifact、Plan、Group、Invocation、恢复和终态。
- Runner Trace 包含模型耗时、token 使用量、状态与错误码。
- 内部财务调用产生成功 Invocation Link；图外营销调用产生拒绝事件。
- 容器日志扫描：密钥格式命中 0，Authorization Header 命中 0。
- ExecutionEvent payload 扫描：密钥格式命中 0，Authorization 字样命中 0。
- 注册表仅保存环境变量引用，API 响应只返回 `credential_configured: true`。

## 7. 缺陷

### UAT-DEF-001 — P1 — 草稿测试无法创建带 Skill 的 sealed 快照

- 修复状态：**已关闭**。复制 Tool/Skill/Binding 后先独立 flush，再设置 `immutable_at`；没有删除或放宽不可变触发器。
- 回归证据：SQLite 使用等价 sealed-relation trigger 验证 flush 顺序；Docker Mock 全栈在真实 MySQL 8 上创建带已发布 Skill 的 draft-test snapshot 和 Run 成功。

- 现象：点击“测试草稿”后提示“草稿测试创建失败”。
- 后端错误：写入 `agent_version_skill` 时触发 MySQL 1644，`sealed agent assembly is immutable`。
- 影响：无法在发布前使用草稿测试验证完整 Skill 装配；正式发布和正式 Chat 可运行。
- 临时规避：发布到隔离环境后通过正式 Chat 验证；不得在生产使用此规避。
- 发布要求：修复不可变触发器与草稿测试快照复制顺序，并补充回归测试。

### UAT-DEF-002 — P1 — 中文 Artifact 文件名下载返回 500

- 修复状态：**已关闭**。下载响应使用 ASCII `filename` fallback，并按 RFC 5987/6266 增加 UTF-8 `filename*`；同时移除 CR/LF 和不安全 fallback 字符。
- 回归证据：中文、空格、引号文件名响应头单测；Docker 全栈实际写入、发布并逐字下载 `验收报告 2026.txt`，内容与 UTF-8 响应头均通过断言。

- Artifact：`019fc586-1ac3-7464-9da4-7d6b82973911`。
- 现象：Artifact 已发布，下载接口返回 HTTP 500。
- 根因：Starlette 生成 `Content-Disposition` 时用 latin-1 编码中文文件名，抛出 `UnicodeEncodeError`。
- 影响：中文业务报告无法从 UI 下载，破坏 Artifact 交付闭环。
- 临时规避：使用 ASCII 文件名并在正文保留中文标题。
- 发布要求：按 RFC 5987/6266 输出 `filename*`，并覆盖中文、空格和特殊字符测试。

### UAT-DEF-003 — P2 — 上游 streaming 偶发 incomplete chunked read

- 修复状态：**代码路径已关闭，Provider 稳定性继续观察**。`peer closed connection` 和 `incomplete chunked read` 被纳入零输出瞬时错误；只在尚未产生 text/tool payload 时重试一次，已有 delta 后仍禁止重放。
- 回归证据：精确错误文本的零输出恢复单测，以及已有 delta 不重试单测。30 次真实 Provider Proposal 稳定性观测仍属于发布前运行证据，不用单测伪造。

- 现象：第一次 Plan Proposal 和一次额外 Group Formation 失败。
- 错误：`peer closed connection without sending complete message body (incomplete chunked read)`。
- 影响：Proposal 生成稳定性下降，但受控重试可成功。
- 临时规避：用户手动重试；保留失败 Run/Trace。
- 建议：增加受限自动重试、明确 UI 重试提示和 provider 级错误码。

### UAT-DEF-004 — P2 — Group History 检索未命中相关上下文

- 修复状态：**已关闭代码缺陷**。空白分隔查询改为多词 AND 匹配，各词可出现在同一条可见 Event、Message 或 Artifact 的不同位置；原 Group/direct 可见性、游标和上界约束保持不变。
- 回归证据：查询 `华东仓 取消率` 能命中 `华东仓订单取消率由3%升至11%`，同时继续拒绝发给其他 GroupNode 的 direct 事件。首次 UAT 的 Formation 数据污染不作为修复证据。

- 现象：查询“华东仓 取消率”扫描 58 条记录但返回 0 hits，虽然 Bootstrap 和多个 Agent 输出包含相关事实。
- 影响：Agent 不能按要求引用共享历史命中，只能回退引用 Bootstrap。
- 临时规避：拆分查询词或显式引用 Bootstrap。
- 建议：明确 Bootstrap、共享消息与 ExecutionEvent 的索引范围，并补充中文分词/多词查询测试。

### UAT-DEF-005 — P2 — 前端容器健康检查误报 unhealthy

- 修复状态：**已关闭**。Compose 健康检查固定访问容器 IPv4 loopback `127.0.0.1:80/healthz`，避免 `localhost` 优先解析到未监听的 IPv6。
- 回归证据：原 `test-frontend-test-1` 重建后为 `healthy`、FailingStreak 为 0，容器内探测持续返回 `ok`；Chromium UI 全栈测试通过。

- 现象：宿主机 `http://localhost:8081` 返回 200，页面可正常完成 UAT；容器健康检查持续连接拒绝。
- 影响：编排层 readiness 信号不可信，可能误触发重启或阻断部署。
- 临时规避：本地复核使用宿主机 HTTP 探测。
- 建议：对齐健康检查端口、监听地址和启动时序。

## 8. 遗留风险与发布建议

- UAT-DEF-001、UAT-DEF-002 已修复；正式发布仍必须在固定 Commit 上通过完整门禁并提供自建 CI Run/Artifact 链接。
- 对 UAT-DEF-003 做至少 30 次 Proposal 压测并统计重试率；本次单次成功不能代表稳定性达标。
- 修复 UAT-DEF-004 后重新验证 Group 共享记录引用和检索权限边界。
- 使用不包含仓库名称的全新 Group Formation 重跑定向澄清，排除本次测试数据预污染。
- 修复前端健康检查，确保容器状态与用户可用性一致。
- 本次没有 CI Run、构建产物签名或部署审批链接；不得把本地 UAT 报告冒充正式发布证据。
- 验收完成后轮换测试模型密钥。

## 9. 复核入口

- [验收证据索引](evidence/index.md)
- [零售证据分析 Skill](assets/retail-evidence-analysis/SKILL.md)
- UI：`http://localhost:8081`
- API：`http://localhost:8001`

现场按要求保留。复核人员应使用独立授权渠道获取测试账号，不得在文档中维护凭证。
