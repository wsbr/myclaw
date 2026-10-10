# Plan 模式

本文是 Plan 模式的产品语义、权限边界、WorkingPlan、用户批准和执行衔接的唯一权威来源。

## 定义

Plan 不是独立 Agent 类型，也不是把自然语言编译成固定 DAG 的工作流引擎。它是同一 Agent Session 上的实施前交互与权限模式：

```text
同一个 Agent Loop
        │
        ├── Chat：可以直接行动
        └── Plan：先调查和规划，写入/副作用暂时只读
```

用户选择 Plan，是在表达：

> 先理解问题、检查环境、提出方案并和我对齐；在我批准前不要实施。

## 规划能力不属于 Plan 模式

任何模式中的 Agent 都可以根据复杂度自主规划：

- Chat 可以先生成计划再直接执行；
- Plan 必须先展示计划并等待实施批准；
- Group 的每个成员都可以维护自己的计划。

因此，`WorkingPlan` 是通用 Agent 能力，`Plan Mode` 是权限和交互策略。平台不能因为当前是 Chat 就禁止计划，也不能因为当前是 Plan 就强制使用固定节点。

## Plan 生命周期

```text
用户进入 Plan
    ↓
Session permission_mode = plan_readonly
    ↓
Agent 读取、搜索、分析、澄清、按需调用只读能力
    ↓
Agent 创建或更新 WorkingPlan
    ↓
向用户交付当前方案
    ↓
用户：批准 / 修改 / 继续讨论 / 取消
    ↓ 批准
permission_mode 切回可执行模式
    ↓
同一 Session、同一 Agent 基于已有上下文实施
    ↓
根据新事实继续更新 WorkingPlan
```

批准不创建另一套 PlanExecution，也不把 WorkingPlan 编译为 WorkItem DAG。

## `plan_readonly` 权限

Plan 模式默认允许：

- 阅读 Workspace 和项目文件；
- 搜索代码、历史、文档和已授权知识；
- 执行被明确标记为只读且无副作用的检查；
- 查询 Agent Network 和候选 AgentCard；
- 向用户发起 Clarification；
- 调用只读研究型 Agent，前提是当前 Collaboration Policy 允许；
- 创建和修改 WorkingPlan；
- 产生分析消息、风险和方案 Artifact 草稿。

默认禁止：

- 修改项目文件；
- 写入外部业务系统；
- 发布正式 Artifact；
- 发送不可撤回通知；
- 执行会改变环境状态的命令；
- 以用户批准方案为名扩大 Tool、数据或 Agent 权限。

权限根据 Tool 的真实语义和参数判断，不根据 Tool 名称或模型文字猜测。用户仍可以单独批准某个精确 Action，但单次批准不等于批准整个方案。

## WorkingPlan

WorkingPlan 是 Agent 主动维护的可见工作状态：

```text
summary
items[]
  ├── text
  └── status: pending | in_progress | completed
explanation?
revision
updated_at
```

它用于：

- 帮助模型在复杂任务中保持方向；
- 让用户看到当前理解和准备怎么做；
- 跨 Context Compaction 保留高信号进度；
- 在失败或新证据出现时明确调整方向。

它不用于：

- 自动生成 ToolInvocation；
- 自动调用计划里提到的 Agent；
- 作为业务授权清单；
- 决定 Run 是否成功；
- 要求每个 item 对应数据库 WorkItem；
- 限制 Agent 只能按顺序执行。

计划更新使用小型 `update_plan` Runtime Coordination Tool。该工具必须在 Agent 草稿中显式绑定并发布，平台必须启用；缺少有效绑定时以 `working_plan_tool_unavailable` 失败，不自动注入、不循环要求模型生成无法执行的调用。已绑定的 `update_plan`、`ask_user` 在 `plan_readonly` 中允许维护计划和澄清，业务写入仍须等待批准。第一阶段 Provider 必须通过 native Tool Calling Conformance；不支持 Tool Calling 的 Provider 不能用于三模式 UAT，也不能通过解析文本、映射列表或 Adapter 伪造 PlanUpdate。未来若支持其他结构化 Action 协议，必须先作为新的可观测 Provider 能力进入 Model Conformance，而不是 Plan 模块的隐式兼容路径。

## 用户批准什么

用户批准的是：

- 当前方向已经足够合理；
- Agent 可以退出只读模式开始实施；
- 后续行动仍受当前身份、Tool、Sandbox、数据和高风险确认策略约束。

用户没有批准：

- 计划永远不能变化；
- 计划列出的所有副作用都自动放行；
- Agent 可以调用任意新 Agent；
- 计划文本可以覆盖平台权限；
- 模型可以把未执行事项声称为完成。

批准事件至少记录 `session_id`、当前 WorkingPlan revision、用户、时间和批准后的 permission mode，用于解释而不是冻结认知。

## 修改、拒绝和继续规划

用户可以直接用自然语言修改方案，也可以在 UI 编辑 WorkingPlan。修改进入 Session 作为新用户输入，由 Agent重新检查并产生下一 revision。

用户拒绝后仍处于 `plan_readonly`。Agent 可以：

- 根据反馈修改方案；
- 补充调查；
- 进一步澄清；
- 给出替代方案；
- 承认当前无法形成可靠计划。

用户取消则结束当前 Plan Run，不修改 Workspace。

## 批准后的实施

批准后仍使用同一 Agent Loop、Session History、WorkingPlan、Invocation 结果和 AgentVersion。Agent 应根据真实执行反馈修订计划，而不是机械执行批准时的文本。

以下变化通常不需要重新批准整份 Plan：

- 调整步骤、顺序和并行方式；
- 修复测试发现的缺陷；
- 重复使用仍授权的 Tool；
- 更换等价实现；
- 更新风险和完成状态；
- 在已授权的发现与协作范围内选择更合适的 Agent。

以下变化仍需要相应权限确认：

- 新的高风险或不可逆副作用；
- 扩大数据访问范围；
- 使用未授权 Tool、Credential 或 Sandbox；
- 邀请超出 Collaboration Policy 的 Agent；
- 明显改变用户目标或承诺的交付范围。

这类确认沿用通用 Approval，不另建 Plan Amendment 领域系统。

## Plan 中的 Agent 协作

Plan 的当前 Agent 仍对用户负责。它可以在调查或实施时按需调用工作网中的专业 Agent：

```text
用户 ↔ 当前 Agent
          ├── discover / invoke → 产品 Agent
          ├── discover / invoke → 开发 Agent
          └── discover / invoke → 测试 Agent
```

被调用 Agent 的结果返回当前 Agent。它不是 Group 成员，用户也不会自动进入其独立上下文。需要成员直接通信或用户直接介入多个 Agent 时，应建立 Group。

Plan 使用同样的两条协作授权路径：显式绑定允许 A→B→C 逐跳委派，不要求开启群组网络；双方开启 `group_enabled` 时，也可在原始用户授权范围内无需绑定进行网络发现与委派。每一跳使用当前调用方自己的授权，不把 B→C 传递为 A→C。`discover_agents`、`invoke_agent` 等仍须在对应 AgentVersion 显式绑定，`plan_readonly` 限制继续传递到子调用。

Plan 不冻结“参与者集合”。Agent 搜索和调用谁取决于当前 Collaboration Policy、权限、预算和任务判断。第一阶段 UAT 可以只开放产品、开发、测试三个候选 Agent，以控制变量，但这是部署配置，不是 Plan 产品语义。

## 停止与结果

在 `plan_readonly` 阶段，模型返回无 Tool Call 的最终方案时：

- 当前 Turn 结束；
- 方案显示给用户；
- Session 等待批准、修改或取消；
- 不把“计划已经写完”误判为业务实现完成。

批准实施后，最终消息按普通 Chat 的自然停止语义交付。平台不要求 `OutcomeClaim` 或通用 Evaluator。

如果场景存在机器可验证标准，例如测试、文件或 Artifact，UI 可以展示 verified 状态；没有真实执行记录时，Agent 只能把结果描述为未验证、部分完成或建议，不能伪造通过。

## 用户体验

用户应能：

- 随时在 Chat 和 Plan 之间切换；
- 看到当前 permission mode；
- 查看 Agent 的调查消息和 WorkingPlan revision；
- 回答 Clarification；
- 批准并选择实施权限级别；
- 继续要求修改而不丢失上下文；
- 批准后看到计划随执行动态更新；
- 暂停、取消或要求提前汇报；
- 查看 Tool、测试、Artifact 和失败证据。

## 第一期 UAT

项目经理 Agent 的标准路径是：

1. 用户以自然语言给出容量风险计算器目标并进入 Plan；
2. Agent 在 `plan_readonly` 下检查仓库和需求、必要时澄清；
3. Agent 自主生成 WorkingPlan，而不是 Formation/DAG JSON；
4. 用户审阅并批准实施；
5. 原 Session 切换可执行权限；
6. 项目经理按需调用产品、开发、测试 Agent；
7. 新事实触发 WorkingPlan 更新；
8. 真实程序、精确九例和 Artifact 形成后自然交付；
9. 取消、Worker 重启和 Approval 恢复不丢失状态。

验收关注目标完成和 Agent 自主适应，不要求每次运行产生相同计划步骤或协作顺序。

## 禁止设计

- 为 Plan 建立独立 Agent Runtime；
- 用户长请求自动切换成显式 Plan 模式；
- 普通 Chat 禁止模型生成计划；
- 把 Plan 固定为不可变 DAG；
- 根据 Plan item 自动调度 Tool 或 Agent；
- 把计划批准等同于所有副作用批准；
- 冻结永久参与者集合；
- 用 `GoalEnvelope`、`OutcomeClaim` 或强制 Evaluator 作为通用前后置协议；
- 批准后创建失去原上下文的新 Session。
