# 执行数据隐私清理：P3a.2

**状态：2026-10-24 完成本地验收，由 `c96b4e7` 推送且远程 CI 成功。** P3a.1 已为固定研究子调用提供在线隐私门禁，P3a.2 补齐遗忘后的执行副本清理和恢复残留检查。[原本地证据](evidence/p3a2-implementation.json)区分首次完整运行、关联进程用例复跑、脚本和部署验证；[远程运行](https://github.com/westbrook0904/FinanceClaw/actions/runs/40055663280)整体 `completed/success`，1,663 项 Java 测试和 41 项部署检查通过。P3a.3 的私有叶子 scope 扩展已单独完成验收并推送 `d433423`，远程 CI 成功，见文末。

## 清理哪些数据

一次模型执行会把同一份信息留在多个位置。只提升用户隐私 epoch，虽然能阻断正常读取，却不能证明步骤回执、检查点或工具文件中的旧正文已经清除。

| 数据 | 本模块的处理 |
| --- | --- |
| 根 Agent、固定研究子 Agent 的旧 SDK 检查点 | 清空 payload，设置删除墓碑并递增 version；覆盖全部历史 attempt |
| `business.capability_steps` | 删除旧来源的确定性步骤及研究结果回执 |
| `business.child_invocations` | 删除旧来源的子调用身份与预算记录 |
| `business.artifacts` 中 `privacy_bound=true` 的工具文件 | 删除受遗忘边界约束的行及正文 |
| 当前有效 epoch、其他 owner 的执行资料 | 保留；损坏的跨 owner 归属不能作为保留理由 |
| 已有合法 SDK 墓碑 | 保留原版本，不因重复清理再次推进 |
| 业务 Turn、Journal、报告、普通 Artifact | 继续按既有业务策略保留；不声明账户数据全删 |

画像、证据、Turn 记忆快照、会话工作上下文及记忆作业继续由原遗忘流程处理。本模块没有新增隐私版本、子任务生命周期或后台清理轮询。

## 在同一遗忘事务中清理

普通遗忘与独立遗忘账本的恢复重放使用同一执行数据清理契约，各自持有原有 owner 锁，在本次数据库事务内完成清理。SDK 写回继续通过父执行门禁核对 owner、epoch、授权及 fence，不能在清理提交后写回旧正文。

SQL 按根 Turn、Command 和**全部历史 attempt**派生状态位置，不只读取当前 Command 指向的 attempt。派生算法与 `ExecutionScope.hash` 一致：依次拼接每段 UTF-8 正文的四字节大端字节长度和正文，再计算 SHA-256。多字节字符、含冒号的 call ID 和父／子 scope 均按原身份计算，不能用分隔符拼接或字符长度代替。

执行顺序先将已定位的旧 SDK 状态变为墓碑，再删除步骤、子调用和隐私绑定工具文件。删除调用记录在前会丢掉子 scope 的定位依据；直接删除状态行会破坏 CAS 拒绝旧版本的能力。没有调用绑定的 `child:*` 步骤仅用于定位待擦除状态，不恢复其预算或执行权。

同 owner 仍有无法证明属于有效新来源的 live SDK 状态时，清理函数抛出错误并回滚本次事务，不能把残留计数伪装成零。完全无法确定 owner 的 live 状态由全局恢复检查阻断。错误不授权推测正文、删除其他用户数据或解除原隐私屏障；应保持隔离，核对来源与运行身份后再执行受审查的修复。

## V7：集合函数，不增加表

[V7](../../src/main/resources/db/migration/V7__execution_privacy.sql) 增加四个 `SECURITY INVOKER` 函数及三个部分索引，主库仍为 **41 张应用表**：

| 函数 | 责任 |
| --- | --- |
| `agent_runtime.scope_hash(text[])` | 与 Java 执行身份一致的 UTF-8 长度前缀摘要 |
| `agent_runtime.execution_scopes()` | 从全部历史根 attempt 和固定研究调用派生可定位 scope |
| `agent_runtime.unsafe_execution_data()` | 只返回残留的 owner 和类别，不返回 SDK 或工具正文 |
| `agent_runtime.erase_execution_data(tenant, subject, before_epoch)` | 在调用方 owner 锁及事务内执行集合清理和残留门禁 |

执行清理由数据库集合操作完成，不在 Java 逐个历史 attempt 查询或逐条删除。普通遗忘按 owner 限定范围；`states_live_user` 只覆盖有正文状态。工具文件分别从根 Turn 的 owner、Artifact 声明的 owner 两个入口收集候选，使用 `artifacts_private_turn` 和 `artifacts_private_owner` 部分索引，合并 ID 后按主键删除，避免把全体用户的 Artifact 作为遗忘候选。

全局恢复核对复用同一残留函数，Snapshot 的四类运行计数按 owner 一次聚合，再与主体集合关联，不逐用户反复扫描残留集合。未知运行行仍必须显式计入异常，不能通过关联缺失被过滤掉。这些结构限制查询范围，不代表已经测得生产并发容量。

V7 由独立 `init-db` 应用，业务进程仍关闭 Flyway，受限 Memory Worker 不获得业务或 AgentState 访问权限。已有 Java 数据库的升级流程见[初始化与升级](fresh-schema.md)。

## 恢复快照必须使用 v3

[导出 SQL](../../ops/restore-privacy-snapshot.sql) 在只读一致性事务中生成 `schema_version: 3`。除原有画像、来源、事实、投影、快照、作业与遗忘审计检查外，每个 owner 必须具有以下四个非负整数计数：

| 字段 | 检查范围 |
| --- | --- |
| `unsafe_sdk_states` | 旧 epoch、来源异常、缺少绑定或无法定位的 live SDK 状态 |
| `unsafe_capability_steps` | 旧来源、归属异常或没有调用绑定的子步骤 |
| `unsafe_child_invocations` | 旧来源或无法证明有效归属的子调用记录 |
| `unsafe_tool_artifacts` | 隐私绑定工具文件的旧来源或身份不一致 |

顶层 `orphan_runtime_rows` 检查无法对应有效 owner 的执行残留，包括 user hash 也无法关联的 live SDK 状态。已清空正文的合法墓碑不计为残留；普通业务产物不因本模块扩大为隐私绑定文件。

[比较器](../../scripts/check-restore-privacy.py) 要求双方均为完整 v3 快照，四类残留及孤立运行行必须为零，原有权威遗忘事实、epoch 和清理完成检查继续有效。v1/v2 或缺字段输入返回 `incomplete`；不能给旧 JSON 补零冒充新导出。必须先在隔离库应用 V7，再用当前 SQL 从最新权威库与恢复库分别重导快照。

恢复重放的 `unsafeRows` 使用同一执行残留来源；仅让 epoch 与账本一致，不足以得到 `privacy_replayed`。Snapshot 比较成功也只证明所提供快照在本专项中的一致性，不能证明参考状态最新、全部业务数据完整或已经可以接入生产。操作顺序见[运行维护](../operations.md#备份和隐私恢复)。

## 验收边界

本地已通过 **1,226 项单元／契约／架构测试、437 项集成用例的最新结果、46 项脚本测试及最终 41 项 Compose 检查**。集成初跑 437 项中有 2 项进程等待超时，其余 435 项通过；同一 JAR 复跑相关 5 项全部通过，逐用例核对覆盖两处失败。这里不将多次运行表述为单次完整 Maven verify 全绿。

Compose 首轮通过 40 项后，末尾旧断言仍要求遗忘后保留 `publish_report` 步骤而失败；备份实测该步骤已清零。只修正验证脚本后，完整重跑 41 项通过并完成隔离环境清理。生产 Java、SQL 和 JAR 没有因这些复跑改变；原始失败、复跑报告和摘要均保留在[本轮证据](evidence/p3a2-implementation.json)。

验证覆盖普通遗忘与账本重放的原子清理、全部历史根／子 attempt、墓碑和旧 CAS、有效新 epoch／其他 owner 保留、未知状态阻断，以及严格 v3 快照、受限角色和 41 表备份恢复。模型为离线或本机模拟，未认证真实外部供应商或生产容量。

P3a.3 的[同 owner 空工具叶子委派](private-agent-delegation.md)已推送 `d433423`，[远程 CI](https://github.com/westbrook0904/FinanceClaw/actions/runs/40065079071)整体成功：1,702 项 Java、46 项脚本和 44 项部署检查通过。V8 将既有集合映射扩展到冻结的目标 Agent/release，私有 scope 与 Java 派生式一致；失效绑定即使没有 SDK 状态也计入恢复残留，原先先墓碑后删除及未知 live 状态阻断的契约保留。本轮通过 1,702 项 Java 用例的最新分次结果、46 项脚本及 44 项 Compose 检查；原完整 verify 的两项失败与精确复跑记录见[P3a.3 证据](evidence/p3a3-implementation.json)，不声称单次完整 verify 全绿。P3a.2 的历史证据不改写为 P3a.3 结果。

P3a 整体、通用子工具装配、递归、RAG 与 Scheduler 不属于本模块。

P3a.6 的[固定行情叶子](private-agent-market-leaf.md)通过 V9 将集合核验扩展到精确市场计划与一次工具上限，继续复用现有 scope 和先墓碑后删除流程；无效绑定仍保留原始定位字段。表数量保持 41，不新增隐私状态权威。该模块的本地和远程状态以专项记录为准。
