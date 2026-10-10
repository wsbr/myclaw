# UAT 基础修复交付记录（2026-09-22）

状态：已修复并部署到 `myclaw-uat`，数据库 Schema 为 **0051**。

发布：`uat-infra-20260922-9fdf126d07304fa6`。
镜像：`myclaw-uat-infra:9fdf126d07304fa6`。
镜像 ID：`sha256:03dc2509437b98799b95d71b808cccee7b6147601c6b7297d13bc8d3c452ebef`。

本次使用原 UAT 镜像作为基础，覆盖经过测试的 16 个源文件/迁移文件。已逐文件验证 API 与所有 Worker 共 11 个容器的文件哈希。原 UAT 与当前 Git HEAD 存在的三处 Context Policy 既有差异保持原状；发布证据记录原镜像、补丁文件、源码哈希和回滚镜像。工作区代码保留供评审，未创建 Git 提交。

## 已完成

1. **后台清理恢复。** 新增 0050 迁移，允许受审计的过期终态 checkpoint 批次删除。数据库继续拒绝 checkpoint 更新，以及无本批允许列表、活跃父任务或保留期内的数据删除。审计允许列表与删除同事务，批次审计自身不可改删。清理类别独立提交，部分失败不会回滚其他类别；审计 flush 失败也不会泄漏维护锁。迁移先安装新保护再移除旧保护。
2. **文件读取边界。** 修复 UTF-8 中文/emoji、UTF-16 大小端与代理对的分页截断。offset 保持字节语义，最多补读 3 字节完成字符，next_offset 精确推进；非法 offset、编码及不存在文件分别返回明确错误码。UTF-16 文件要求 BOM。
3. **确定性文件冲突不再原参数重试。** create/replace/checksum 冲突立即反馈，保留 checksum CAS，由后续新动作读取和解决冲突。
4. **Span 投影并发幂等。** 新增 0051 可空唯一投影键，数据库取得写入权后再导出 OTEL。同一事实版本并发重放不产生新投影；不同版本、普通 Span 正常保留。历史重复 Span 与 links 不做去重删除，过期记录仍按原留存规则清理。
5. **Provider 计量与错误诊断。** OpenAI/Anthropic 缓存 token 贯通到 ledger，区分未提供、明确零、正数；超时归类为结构化错误码。工具参数 JSON 异常保留已知 usage、request ID、结束原因及脱敏解析位置/长度，不新增已有 payload 的透明重试。

## 验证

| 检查 | 结果 |
|---|---|
| 后端完整单测 | **472 passed** |
| 独立 MySQL 集成 | **8 passed**：留存 5、并发投影 2、账本约束 1 |
| 迁移 | 隔离 MySQL 迁移升级验证通过；UAT 0049→0050→0051 成功 |
| 活跃/新近数据保护 | 拒绝无授权批次、未过期 checkpoint、活跃 WorkItem/Run、新近完成 Run；审计与 checkpoint 修改保护通过 |
| 维护锁异常路径 | 强制真实 ORM audit flush 失败后，连接池未遗留锁；调用方已持锁时不重复获取 |
| 时间精度 | MySQL 微秒 cutoff（.8 秒）边界回归通过 |
| Span 并发 | 8 个独立 MySQL 事务同步竞争，同一事实仅一条投影与一次导出 |
| UAT 就地文件读取 | 真实已部署函数的中文/emoji 100 字节分页拼接通过 |
| UAT 健康与版本 | API `/health`、前端 `/healthz` 均 200；10 个 Worker 新版本心跳正常；11 个容器补丁哈希一致 |
| 文档/补丁格式 | 文档链接检查、`git diff --check` 通过 |

未调用付费模型做新业务回放。管理端认证读接口冒烟未完成：部署环境的初始管理员密码返回 401；已将账号密码 hash 与部署前备份比对一致，没有修改已有账号、重置密码或绕过登录。健康、数据库、迁移和 Worker 检查已完成。

## UAT 留存恢复结果

第一轮由新 maintenance-worker 启动自动执行；随后执行两轮默认 500 条上限的清理，再执行一轮空积压验证。四轮总审计均为 `succeeded`、无失败类别。首轮恢复成功时间为 **2026-09-22 14:44:26（UTC+8）**。

| 表 | 部署前 | 清理后 | 变化 |
|---|---:|---:|---:|
| runner_checkpoint | 1,343 | 286 | 过期终态快照清理 1,057 |
| outbox_command | 1,226 | 280 | 过期完成命令清理 946 |
| runner_trace | 574 | 122 | 过期 Trace 清理 452 |
| observability_span | 4,645 | 1,007 | 过期 Span 清理 3,638 |

符合策略的过期 checkpoint 剩余 **0**。受保护业务事实数量一致：Run 231、WorkItem 284、ExecutionEvent 4,725、ProviderAttempt 829、ToolInvocation 303、Artifact 14、UsageLedgerEntry 1,559。

## 证据与回滚

本地证据目录（Git 忽略）：`deploy/uat/evidence/infra-fix-20260922/`，包含：

- `manifest.json`：原镜像、发布镜像、补丁哈希、部署容器、测试与验证边界。
- `patch/`：实际构建用的源文件和 Dockerfile；`changes.patch`：工作区差异。
- `compose.override.yml`、`compose.rollback.yml`：本批修复及原 UAT 镜像集合。
- `before-counts.json`、`after-counts.json`、`retention-results.json`：清理前后对账和四次成功审计。
- `unit-tests.log`、`integration-tests.log`、`build.log`：测试与构建证据。

已创建逻辑备份及 SHA256：`deploy/uat/backups/myclaw-uat-20260922T064137Z.sql.gz`。原应用镜像保留为 `myclaw-uat-rollback:20260922-<service>`。若回滚应用，应使用原镜像覆盖文件并保持 Schema 0051；不要在运行环境 downgrade 或覆盖还原数据库。旧 maintenance-worker 不支持新批次协议，回滚时应暂停该维护服务，避免重新产生清理错误。

正常重新部署仍可从包含这些修复的工作区构建。复现本次精确补丁版本时使用证据中的 base image、patch 文件和 compose override，而不是假设其等同于当前 Git HEAD 全量镜像。

## 仍需后续处理

- 详细方案场景的模型输出截断/非法 JSON：本次补齐诊断，未声称解决供应商输出质量或实现自动修复。
- 澄清与正式审批重复交互、审批截止时间展示、UI 轮询退避。
- 宿主机合盖休眠：需要持续在线部署环境；本次未修改宿主电源设置。
- 实际价格快照尚未配置，费用继续如实显示 unknown；历史账本与旧缓存计量不回写。历史 Span 报表仍需按事实版本去重。
