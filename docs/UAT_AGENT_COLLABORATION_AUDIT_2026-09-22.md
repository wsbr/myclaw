# UAT Agent 协作边界核实（2026-09-22）

本文件保留修复前的审计证据；后续实现和验证记录见[两种协作方式修复](UAT_AGENT_COLLABORATION_FIXES_2026-09-22.md)。

本次只核实协作语义、UAT 历史调用和授权路径；没有修改运行代码、Agent 配置或 UAT 数据。

用户要求区分两种协作：沿显式绑定关系逐级委派的固定协作，以及加入协作网络后发现其他 Agent、自由委派或组群的涌现协作。当前实现没有严格区分这两个边界。

用户进一步明确：A 绑定 B、B 绑定 C 时，允许 A → B → C。第一种协作并不限于一层；每一跳检查当前调用者自己的显式绑定。A 不需要直接绑定 C，也不会仅因 B 绑定 C 而获得直接调用 C 的权限。整条显式绑定链不要求参与 Group。

用户对第二种协作的定义：开启 Group 就是加入 Agent 协作网络，具有双向含义——接受网络中其他成员的委派，也可以发现和委派其他网络成员。A、B、C 均开启 Group 时，彼此不需要预先绑定。网络参与仍在同一租户、原始用户授权和目标治理的有效范围内；这些共同授权条件适用于两种协作。

用户入口是普通对话：用户向 A 提出诉求 → A 判断是否需要其他 Agent → 在网络中寻找合适成员 → 自动组建包含 A 的任务群 → 成员在群内交互、委派并协作完成诉求。用户不需要预先指定成员或手动建群；A 能独立完成时也不必建群。

“加入网络”与“加入某个任务群”必须区分：`group_enabled` 应表达前者，具体 GroupSession 的 Membership 表达后者。网络资格使 Agent 能在建群前相互发现，并非必须已经共享一个任务群才能被发现。

两种协作可以共存：一条调用关系可以凭显式绑定成立，也可以凭双方网络资格成立。关闭 Group 不阻断合法绑定链；没有绑定且任一方未开启 Group 时，不能借助网络绕过绑定。工具绑定仍决定 Agent 是否具有相应操作能力，Group 开关本身不隐式注入工具。

## 核实范围

- UAT 数据时间窗：2026-09-20 07:46:20 至 2026-09-22 07:46:20 UTC，即台北时间 09-20 15:46:20 至 09-22 15:46:20。
- 数据查询只读取 Invocation、调用者冻结版本、目标版本和调用者版本绑定。未读取任务正文、Prompt 或凭证。
- 当前 UAT 的 `agent_invocation_service.py`、`agent_directory_service.py`、`group_collaboration_service.py`、`coordination_tools.py`、`snapshot_resolver.py` 的 SHA-256 均与本地审计文件一致。
- 在隔离内存 SQLite 中调用实际委派服务，验证目标选择和权限分支；这不等价于完整工具分派、模型执行或 UAT 新运行。
- 现有 `test/unit/backend/test_group_network_v2.py`：7 个测试通过。

## 实际语义与预期的差异

| 边界 | 当前实现 | 与用户预期的关系 |
| --- | --- | --- |
| Chat 主子协作 | 根 Agent 未绑定目标时，仍可通过当前用户可调用目录解析目标；子 Agent 不可再次委派 | 既未按绑定限定每一跳，又错误阻断合法的 A → B → C |
| Plan 委派 | 直接解析可调用目录，不检查目标绑定；子 Agent 不可再次委派 | 同样缺少逐跳绑定约束，也阻断合法的多级委派 |
| 网络发现和委派 | `group_enabled` 只是可选发现过滤条件；普通委派不检查双方网络资格 | 缺少双方加入网络这一替代显式绑定的准入条件 |
| 自动建群 | 候选必须允许 Group；发起者未开启 Group 时仍可选其他至少两名合格成员，自己不入群 | 没有保证由已入网的 A 发现成员并与其一起参与任务群 |
| Group 内委派 | 调用者必须具有 joined Membership；目标必须在本轮 GroupRunMember 快照中 | 支持本轮成员之间的协作 |
| Group 扩员 | 本轮 `fixed_for_run`，`invite_agent` 总是拒绝扩员并记录事件；后续 Chat 可重选下一轮成员 | 支持建群和跨轮调整，尚不支持运行中自由扩员 |

### 1. 固定绑定不是执行边界

[agent_invocation_service.py](../backend/app/services/agent_invocation_service.py) 第 167–171 行禁止 Chat/Plan 子 Agent 继续委派；第 205–216 行的 Plan 分支直接调用目录解析器；第 217–237 行的 Chat 分支在目标未绑定时也调用目录解析器。

根据用户对 A → B → C 的补充定义，这里有两个独立偏差：未绑定目标被放行，以及已绑定的后续一跳被一层限制拒绝。不能只补根 Agent 的目标名单而保留对子 Agent 的全面禁止。

这不是偶发遗漏：Chat 代码注释明确允许当前可见 AgentCard 与绑定目标同等成为候选。[agent-assembly.md](modules/agent-assembly.md) 第 110 行也明确将 `bound_agent_ids` 定义为发现快捷方式或受信候选，而非固定拓扑；[agent-collaboration.md](modules/agent-collaboration.md) 第 189 行将固定绑定拓扑列为禁止设计。需要按用户明确的新要求一起调整契约和实现。

### 2. Group 开关没有隔离网络参与

[agent_directory_service.py](../backend/app/services/agent_directory_service.py) 第 33–123 行只在传入 `group_enabled` 时过滤目标；`actor_agent_id` 用来排除自身，不验证调用者的网络资格。第 126–166 行的调用解析器检查租户、发布状态、用户使用权限、卡片可用性和 callable 治理，没有 Group 资格检查。

[coordination_tools.py](../backend/app/runtime/coordination_tools.py) 第 145–152 行将模型可选的 `group_enabled` 原样交给发现服务。第 218–223 行只在当前 Chat Agent 属于候选目录时把它加入选群结果。[group_collaboration_service.py](../backend/app/services/group_collaboration_service.py) 第 76–84 行只过滤被选目标，第 767–770 行允许当前 Agent 不属于新群并回退其他成员作为 reporter。

因此，关闭 Group 的 Agent 在具有可用的 `select_group_members` 工具绑定及完整候选目录等前提下，代码路径仍允许为其他合格 Agent 建群。这项结论来自代码路径核对，未在 UAT 发起新建群运行。

`group_enabled=true` 与已经入群是两个事实：当前实现把前者作为被选候选的资格；后者由具体 GroupSession 的 Membership 表示。按用户明确的目标语义，前者应升级为双向网络参与资格，覆盖接受委派、发现其他成员、主动委派和自动组群，不能只限制被选为群成员。

### 3. 已绑定 Chat 路径存在独立授权问题

Chat 找到 `AgentVersionBinding` 后，会跳过 `resolve_callable_agent`，只继续检查目标发布状态。因此不会执行标准解析器里的原始用户使用权限、AgentCard availability 和 callable 治理检查。

[agent_assembly_service.py](../backend/app/services/agent_assembly_service.py) 第 221–227 行写入目标绑定时也只验证同租户。绑定不应成为绕过当前用户授权或目标治理的通道。

隔离内存数据库验证结果：

| 用例 | 标准解析器 | 委派服务结果 |
| --- | --- | --- |
| Chat，未绑定，双方 Group 关闭，无 Membership | 允许 | 创建目标 WorkItem |
| Plan，未绑定，双方 Group 关闭，无 Membership | 允许 | 创建目标 WorkItem |
| Chat，未绑定，私有目标且调用账号无使用权 | 拒绝 | 拒绝 |
| Chat，已绑定，同一私有目标 | 拒绝 | 创建目标 WorkItem |
| Chat，未绑定，目标 callable=false | 拒绝 | 拒绝 |
| Chat，已绑定，目标 callable=false | 拒绝 | 创建目标 WorkItem |
| Chat，已绑定，目标卡片 unavailable | 拒绝 | 创建目标 WorkItem |

这里只证明委派服务的目标授权缺口；没有声称该隔离验证执行了付费模型或绕过工具绑定入口。所有跨 Agent 调用仍应同时满足工具授权和目标授权。

### 4. Group 支持成员协作，但本轮不能扩员

[agent_invocation_service.py](../backend/app/services/agent_invocation_service.py) 第 238–274 行要求调用者属于当前 Group，目标属于本次运行冻结成员。组内委派还受深度、循环和预算限制。

[coordination_tools.py](../backend/app/runtime/coordination_tools.py) 第 392–412 行的 `invite_agent` 始终调用拒绝路径，返回 `membership_policy_fixed_for_run`。后续 Chat 可以重新选择 GroupSession 未来成员，但当前 GroupRun 不热插拔成员。

## UAT 最近 48 小时的实际记录

共 7 次 AgentInvocation，全部是 Chat。其中 6 次调用者冻结版本的 `group_enabled=false` 且未绑定目标，5 次成功、1 次超时。另 1 次绑定调用成功，属于用户期望允许的固定绑定路径。

以下时间为台北时间（UTC+8），日期均为 2026-09-21：

| 时间 | 调用者 | 目标 | 已绑定目标 | 结果 | Invocation ID |
| --- | --- | --- | --- | --- | --- |
| 17:01:24 | Hard Compaction UAT 040f636fa3 | Platform Tool Real UAT Agent | 否 | succeeded | `01a0c332-d7c1-7fac-9b7a-7c3d53a104d1` |
| 17:06:26 | Hard Compaction UAT 040f636fa3 | Platform Tool Real UAT Agent | 否 | succeeded | `01a0c337-73d7-7f62-a11b-028400577d60` |
| 17:16:40 | Hard Compaction UAT 040f636fa3 | Platform Tool Real UAT Agent | 否 | succeeded | `01a0c340-d053-7c66-bf48-c4fe0d0ca1e0` |
| 17:36:56 | admin测试专用个人助手 | Platform Tool Real UAT Agent | 否 | succeeded | `01a0c353-5feb-7be3-944b-26b265c60e9a` |
| 17:39:34 | admin测试专用个人助手 | Platform Tool Real UAT Agent | 否 | succeeded | `01a0c355-c8be-7d99-92e8-ed9e37f5d710` |
| 17:42:52 | admin测试专用个人助手 | 王孙个人测试智能体 | 是 | succeeded | `01a0c358-cd09-73f4-bc43-fb79fea09b32` |
| 18:09:25 | Hard Compaction UAT 040f636fa3 | Platform Tool Real UAT Agent | 否 | timed_out | `01a0c371-1dc3-76e3-b7f6-b3a8f45abd76` |

这些是 09-21 的历史记录，早于 09-22 的内置工具统一绑定部署。新部署已要求显式绑定 `invoke_agent` 等工具，但没有改变上述目标 Agent 的授权逻辑，因此不能把工具入口修复当作本问题已经修复。

## 建议统一的契约与修复次序

1. **先补当前授权缺口。** 所有绑定和非绑定调用都必须经过相同的原始用户权限、当前发布状态、可用性与 callable 治理校验；绑定只能限定候选，不能提升权限。
2. **将固定绑定落实为逐跳目标名单。** 未参与网络的 Agent 只能委派给自己显式绑定的目标；允许 A → B → C 等多级委派，每一跳校验该调用者冻结版本的工具绑定和 Agent 绑定。移除 Chat/Plan 对子 Agent 再委派的一概禁止，改用循环检测、合理的最大深度与共享运行预算控制。原始用户身份沿链传递，各跳均重新检查当前目标授权。A 绑定 B、B 绑定 C 不等于 A 直接拥有调用 C 的权限。
3. **将 Group 开关落实为双向网络准入。** 网络发现、接受委派、主动委派和建群都检查双方的网络参与资格；网络内不要求逐个 Agent 绑定。先判断显式绑定路径或双方网络资格路径是否成立，再应用共同权限及运行约束，不能把 Group 资格强加到固定绑定链上。
4. **实现从普通对话到任务群的完整流程。** 已入网的 A 根据用户诉求自主发现和选择合适网络成员，自动建群并作为成员参与，成员通过群内交互和委派完成诉求。区分首次自动组群与运行中扩员：后者如需支持，应有可审计的邀请、授权及成员版本生效规则。保留 `fixed_for_run` 时，界面与工具说明应清楚说明扩员只能在后续运行生效。
5. **同步文档、界面及测试。** 当前“可调用智能体”界面暗示严格名单，但契约反对固定拓扑；需要统一。补 Group 全关闭时 A → B → C 逐跳绑定成功、缺少逐跳绑定且不满足网络资格时拒绝、双方入网但未绑定时允许发现与委派、普通对话由 A 自动选人建群且 A 留在群内，以及未入网发现／建群拒绝、绑定不绕过权限、循环／深度／共享预算、Group 内合法 peer 协作及扩员边界的回归用例。

本报告记录事实与修复建议，不替代正式产品契约，也未实施上述行为变更。
