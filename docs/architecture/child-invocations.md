# 固定市场研究子调用：P3a.1

P3a.1 给现有 `market_research` 子 Agent 增加持久调用身份、累计预算和检查点访问门禁。P3a.1 已于 **2026-10-24 完成本地验收并推送 `5d82f24`，远程 verify/delivery 均成功**。提交前本地通过 1,226 项完整单元/契约/架构测试、109 项定向 PostgreSQL 测试及 39 项 Compose 检查；109 项来自 10 个指定 suite，未运行完整 Maven verify，见[原本地证据](evidence/p3a1-implementation.json)。远程最终结果见文末，后续 P3a.2 的执行数据清理也已完成本地验收并推送；P3a.3 私有叶子委派已推送 `d433423` 并通过远程 CI，阶段状态见[实施方案](platform-implementation-plan.md#11-p3a1-固定研究子调用)。

## 使用范围

父 Agent 通过已有 `market_research` 工具发起同步研究，最大深度为一。目标固定为市场研究执行器，子工具集只有 `market_snapshot`，子权限固定为 `market:read`。父调用必须同时具有 `research:run` 和 `market:read`；证券范围来自本次研究输入，不能在子调用中扩大。

工具输入示例：

```json
{
  "task": "核对指定证券的冻结行情并说明数据局限",
  "symbols": ["AAPL"],
  "analysis_period": "2026-10"
}
```

证券必须存在于基础发布的行情快照中；默认行情为空。缺少证券或期间时沿用持久人工输入交互，回答后使用原 Turn 和工具调用 ID。基础发布允许时，可在[能力组装](agent-composition.md)中选择 `function:market_research`，其依赖会纳入固定计划。默认父模型仍为离线回显，不会根据这段自然语言自行触发研究。

P3a.1 的目标固定为研究执行器。后续 [P3a.3 私有叶子委派](private-agent-delegation.md)另行提供父发布冻结的同 owner 空工具目标，仍为深度一；已完成独立本地与远程验收，不扩大本文件原 P3a.1 验证范围。没有递归调用、独立子队列或子调度 API。研究执行器仍使用原生 `ReActAgent`，未采用生产 `SubAgentTool` 或 `HarnessAgent` 封装。

## 一张调用表，复用根身份与步骤结果

[V6](../../src/main/resources/db/migration/V6__child_invocations.sql) 只增加 `business.child_invocations`，主库从 40 表增至 41 表。唯一身份为 `(root_turn_id, call_id)`；更换根 attempt 不会建立同一调用的新额度。

| 持久字段 | 责任 |
| --- | --- |
| `root_turn_id`、`call_id` | 根请求和稳定工具调用身份；删除根 Turn 时级联删除 |
| `target`、`input_hash`、`scopes` | 固定 `market_research`、规范化输入 SHA-256 和固定只读权限 |
| `status` | 仅 `pending` / `completed`，表示是否已有通过校验的最终回执 |
| `model_calls`、`tool_calls` | 累计模型请求与首次实际行情读取计数，上限分别为 4 和 10 |

表中不存输入正文、owner、conversation、发布、epoch、grant revision 或第二份结果。读取时从根 `business.turns`、同 owner 的 `memory.sources` 和 `business.turn_policies` 派生身份；来源的 `tenant/subject` 必须与根 Turn 一致，缺失或错误关联不能借当前身份补齐。

结果继续保存于 `business.capability_steps`，命名空间为 `child:<call_id>`，最终步骤为 `result`。`ChildInvocation.resultHash` 是该结果步骤的**输入摘要**，用于核对调用绑定，并非结果正文 SHA-256。`result` 直接 JOIN 现有回执；结果写入和调用 `completed` CAS 在同一事务完成，任何一方失败都回滚。

没有调用绑定的旧步骤不能直接作为新调用缓存，也不能把其未知历史计数当成零。输入摘要改变、已完成调用缺少结果、或结果步骤摘要不一致时均拒绝继续。V6 不回填或猜测已有研究步骤的身份和消耗量。

## 预算与回放

| 动作 | 子预算与根预算 |
| --- | --- |
| 实际子模型请求 | 子模型计数和根模型计数在同一父事务预留；任一上限不足则同时回滚；请求发送后网络失败不返还已提交额度 |
| 首次行情读取 | 先核对固定证券范围，再将子工具计数、行情服务扣减的根工具计数和成功步骤回执原子提交 |
| 同一行情步骤回放 | 先检查父权限和调用身份，再返回既有回执；不执行行情读取、不重复占用这次子/根行情读取额度，并补回本次运行的 evidence |
| 已完成研究回放 | 校验根状态、身份和最终回执后直接返回原结果，不再创建子 SDK 执行器或预留子模型额度 |

父模型重新派发一次 `market_research`，外层仍按正常根工具调用计数；缓存命中不意味着整条父工具链免费。不同工具调用 ID 是不同调用，也不能因为同一证券相同就跨 ID 合并预算。

4/10 上限由数据库 CAS 管理，不再依赖 `ResearchRun` 的内存计数器。重建应用服务或进入新的有效父 attempt 不能重置消耗。根 Turn 的模型/工具上限同时生效，子调用没有额外扩大根预算。

恢复尚未完成的调用时，还会从既有成功快照回执重建 evidence，数量必须与持久 `tool_calls` 一致。即使 SDK 已保存工具结果或最终文本、恢复时没有再次派发行情工具，也能使用先前真实回执校验完成条件；不从模型自行生成的文本推测证据。记录缺失或数量不一致时拒绝继续。

## 父执行门禁与 SDK 检查点

开始、模型预留、行情读取、缓存回放、完成和检查点访问均借用根 `Claim`。`ExecutionPolicyService.dispatch` 在短事务内核对父 Command 的有效租约与 fence、父 Turn 的 `running` 状态、固定发布、隐私版本和当前授权 revision；权限采用发布范围与根受理 scopes 的交集。

`ChildResearchStateStore` 将 SDK 的版本读取、CAS 保存、列表/存在检查和删除接入同一门禁。只有绑定正确且仍为 `pending` 的子调用才能访问检查点；取消、失租、撤权或遗忘先提交后，旧回调不能只凭 SDK user/session 哈希读取或写回旧状态。模型和远程调用仍在数据库事务外进行。

调用身份及步骤回执跨 attempt 保留，SDK 状态 scope 仍包含父 attempt。本阶段不复制子检查点到新 attempt，也不承诺从任意模型流中断位置透明续跑。父请求仍使用现有封存回执与未知执行阻断规则；这张表不能授权重放未知外部副作用。

`pending/completed` 是调用元数据，不替代父 Turn 的完成、失败、取消或恢复权威。没有独立子心跳、租约、轮询或调度器，也不会因为子状态仍为 pending 就自动启动后台补跑。

P3a.1 的验收范围是受治理访问和迟到回调门禁，不包含调用元数据、`capability_steps` 正文及全部子 SDK 状态的完整物理清除。后续 **P3a.2 已补齐这部分清理并完成本地验收**：根据根 Turn、全部历史 attempt 和调用 ID，在 SQL 中使用与 Java 相同的长度前缀 SHA-256 派生根／子状态 scope；先将旧 SDK 正文改为递增版本墓碑，再删除步骤、子调用和隐私绑定工具文件。该次序保留定位依据及旧 CAS 的拒绝能力。

P3a.2 将普通遗忘与独立账本重放接到同一清理契约，并为恢复快照增加四类执行残留与无归属状态核对。新 epoch、其他 owner 及既有合法墓碑保留；无法定位的 live SDK 状态不能假定已清除，必须阻断。业务 Turn、Journal、报告和普通 Artifact 的保留策略没有扩大为账户全删。详见[执行数据隐私清理](execution-privacy.md)。

## 数据库成本与实现入口

调用行、步骤、根 Turn、来源和授权快照都按已有主键定位。开始时读取或插入固定绑定；有已消费行情额度的 pending 调用额外读取其快照回执并校验数量，上限由 10 次读取预算约束。实际模型调用增加一次子预算 CAS；行情缓存未命中时增加子计数与步骤写入；完成时增加一次状态 CAS。读取绑定会 JOIN 根身份和现有最终结果，没有新增按完整会话历史逐条查询或后台扫描。

这些动作仍有 SQL 和父锁成本，次数随实际模型/工具执行增长；它们受根预算及 4/10 子预算约束，不代表零数据库访问或生产并发容量承诺。复用 `GovernanceMapper`，不增加 Mapper、数据源、连接池或结果双写。

| 边界 | 源码入口 | 本轮检查入口 |
| --- | --- | --- |
| 调用身份、计数及原子完成 | [ChildInvocationService](../../src/main/java/com/financeclaw/app/execution/ChildInvocationService.java)、[Gateway](../../src/main/java/com/financeclaw/app/execution/port/ChildInvocationGateway.java) | [ChildInvocationIT](../../src/test/java/com/financeclaw/p3/ChildInvocationIT.java)：持久额度、回放、根预算回滚、原子完成、身份隔离及迟到保存 |
| 根门禁与步骤回执 | [ExecutionPolicyService](../../src/main/java/com/financeclaw/app/execution/ExecutionPolicyService.java)、[CapabilityStepService](../../src/main/java/com/financeclaw/app/execution/CapabilityStepService.java) | 同上：取消、失租、撤权及隐私变化后的缓存/状态访问 |
| 研究与 SDK 状态 | [MarketResearchService](../../src/main/java/com/financeclaw/app/research/MarketResearchService.java)、[MarketResearchAgent](../../src/main/java/com/financeclaw/infrastructure/agentscope/capability/MarketResearchAgent.java)、[ChildResearchStateStore](../../src/main/java/com/financeclaw/infrastructure/agentscope/capability/ChildResearchStateStore.java) | [MarketResearchServiceTest](../../src/test/java/com/financeclaw/app/research/MarketResearchServiceTest.java)：回执重建 evidence、证券范围及门禁传递 |
| SQL、装配与权限 | [GovernanceMapper](../../src/main/java/com/financeclaw/infrastructure/governance/GovernanceMapper.java)、[V6](../../src/main/resources/db/migration/V6__child_invocations.sql)、[RoleConfiguration](../../src/main/java/com/financeclaw/bootstrap/RoleConfiguration.java) | [SchemaInitializationIT](../../src/test/java/com/financeclaw/m5/SchemaInitializationIT.java)、[RoleAssemblyIT](../../src/test/java/com/financeclaw/m5/RoleAssemblyIT.java)、[RuntimePackagingIT](../../src/test/java/com/financeclaw/architecture/RuntimePackagingIT.java) |


## 当前本地验证范围

完整单元/契约/架构测试为 **1,226 项**，零失败、错误或跳过。定向 PostgreSQL 验证当前共 **109 项、10 个 suite**：首次 108 项中，9 个 suite 的 84 项通过，只有 BusinessIT 的旧手写无绑定缓存 fixture 被新门禁拒绝；改为真实 research 流程并新增孤立回执拒绝用例后，BusinessIT 最后 25/25 通过。两次集成运行之间未修改生产实现，不能将这组定向结果表述为一次完整 Maven verify。

同一 JAR 的 **39 项 Compose 检查**已成功并完成隔离环境清理，覆盖故障恢复、权限、遗忘、41 张应用表备份恢复和恢复后的新执行。模型使用离线或本机模拟服务，行情为合成数据；这些结果不认证真实供应商接入或生产容量。冻结报告摘要、三份运行日志、Compose 报告及 JAR SHA-256 记录在[原本地证据](evidence/p3a1-implementation.json)，不改写为后续测试数字。

提交 `5d82f24` 的 [run 40045383841](https://github.com/westbrook0904/FinanceClaw/actions/runs/40045383841) 已整体 `completed/success`：verify 为 **1,226 单元/契约/架构 + 423 集成 = 1,649 项**，delivery 的 **39 项部署检查**通过。该次远程运行不覆盖后续 P3a.2/P3a.3；P3a.3 提交 `d433423` 的 [run 40065079071](https://github.com/westbrook0904/FinanceClaw/actions/runs/40065079071) 已整体成功，1,702 项 Java 测试、46 项脚本和 44 项部署检查通过；其[提交前本地证据](evidence/p3a3-implementation.json)仍保留原完整运行失败与复跑事实。P3a.4 已完成[完整父子入口与取消传播修复](private-agent-runtime-validation.md)的本地验收，未推送，1,242 单元 + 162 选定集成 = 1,404 项通过，新增 9 项包含其中；本轮未重跑其余集成、Compose 或脚本。完整权限私有 Agent 委派、P3a 整体、P3b 和 P4 仍未完成。
