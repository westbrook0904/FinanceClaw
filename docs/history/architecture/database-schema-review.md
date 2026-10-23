> 历史实现记录，保留用于追溯；当前行为见项目 README 和当前架构文档。

> **历史记录**：本文保留当时的设计、命令与验证结果，不代表当前独立应用的部署结构或最新测试结论。当前操作见[运行说明](../../operations.md)，新库结构见[初始化说明](../../architecture/fresh-schema.md)；历史类名、版本号和外部导出命令仅用于理解证据。

# 数据表冗余与 Mapper 命名审查

**当前实施：已新增 V10/V11 迁移，退役四表并将 tool_calls 合入 reports，目标主库为 34 张应用表。Mapper 改名及查询优化已落地，验证状态见第 7 节。下面第 1–6 节保留 V1–V9 的审查依据与未实施候选。**

本次基于当前工作区 V1–V9 迁移、生产 SQL、调用路径、测试和既有设计说明进行静态审查，包含尚未提交的画像和后台查询优化。V1–V9 定义主库 **39 张应用表**：business 20、agent_runtime 2、memory 13、channel 4；独立遗忘账本另有 2 张表。不计 Flyway 元数据表。未查询实际数据库的行数、大小或执行计划，也未修改表结构和生产方法签名。

结论：优先退役 **2 张历史遗留表，以及由另外 2 张表组成的本地投影链路**。真正值得进一步合并的是报告及其本地效果回执；其余一对一表需要按锁、权限和恢复边界判断，不能仅凭字段相同合并。

## 1. 优先退役的表与链路

| 表 | 当前用途与证据 | 建议 |
| --- | --- | --- |
| `business.skill_refs` | 生产无读写；[框架复用说明](../framework-reuse-review.md)已明确改用原生 SkillRepository，旧表停用 | 归档历史数据后，通过新增迁移退役；不合并进其他业务表 |
| `agent_runtime.tool_effects` | 仅 [M0RuntimeMapper](../../../src/test/java/com/financeclaw/m0/support/M0RuntimeMapper.java) 的实验使用；[生产 RuntimeMapper](../../../src/main/java/com/financeclaw/infrastructure/state/RuntimeMapper.java) 只读写 states | 把实验表初始化移入 M0 测试资源，再通过新增迁移退出生产 schema |
| `business.projections` | V2 注释标明是 M1 本地集成投影；生产只有 [TaskMapper.projectBatch](../../../src/main/java/com/financeclaw/infrastructure/task/TaskMapper.java) 写入，未找到业务读取方 | 连同下面的 outbox 链路整体退役，而非继续维护无人消费的三列镜像 |
| `business.outbox` | 当前唯一消费者是 [历史 OutboxProjector](https://github.com/westbrook0904/FinanceClaw/blob/ae16c36/src/main/java/com/financeclaw/worker/OutboxProjector.java)，只把 Turn 状态投影至 business.projections | 当前用途可以退出；不是所有项目中的 Outbox 都冗余，而是本仓库这张表没有承担真实外部投递 |

当前投影链路：

```text
TaskTransitionService：终态写入 business.outbox
  → OutboxProjector 定时调用 projectBatch
  → UPSERT business.projections
  → 标记 outbox.delivered
  → 本仓库无业务读取 projection
```

真实飞书链路使用 [FeishuMapper.dirty](../../../src/main/java/com/financeclaw/infrastructure/channel/FeishuMapper.java) 直接查询 `business.turns`，并通过 `channel.notices` 管理通知快照和发送结果；不读取上述 Outbox/Projection。SSE 则使用持久业务事件重连和进程内 TaskEventHub，也不读取该投影表。

退役链路能省去终态的 outbox 插入、后续 projection 写入、delivered 更新，以及队列的空闲轮询。注意：当前后两种写已合并在批量 SQL 中，不能把三种数据变更误报成每个 Turn 三次独立数据库往返。上一轮批量与退避优化降低了现有链路成本；本轮从业务消费者角度确认它更适合整体退出。

实施时必须同步删除/调整：

1. [TaskTransitionService](../../../src/main/java/com/financeclaw/app/task/TaskTransitionService.java) 的终态 outbox 写入、相关 Gateway/Mapper 方法。
2. ProjectTaskService、OutboxProjector、角色装配、OUTBOX 提示与定时配置。
3. [OperationsMapper](../../../src/main/java/com/financeclaw/infrastructure/task/OperationsMapper.java) 的排空和统计查询；若保持 HTTP 契约兼容，可让原 `pending_outbox` 字段返回 0。
4. 角色/故障/并发测试、Compose 验证、指纹与运维说明。飞书绑定的投影流程继续保留，不能连带删除。
5. 上线前核查仓库之外是否有消费者，以及存量数据归档要求。本结论不能证明外部 BI 或手工脚本没有读表。

不要把 outbox 直接并进 notices：通知还涵盖 waiting/blocked、绑定 revision、suppressed/unknown 等业务语义。未来若新增可靠外部事件发布，应按真实消费者重新设计事件投递表。

## 2. 可以合并，但有前置条件

| 组合 | 具体方案与收益 | 前提与优先级 |
| --- | --- | --- |
| `business.reports` + `business.tool_calls` | 当前 tool_calls 的生产写入仅服务 [ReportService](../../../src/main/java/com/financeclaw/app/report/ReportService.java)；报告和效果回执在同一个本地事务里产生。可统一报告发布记录，将 pending 插入、report 插入、completed 更新三次写合成一次插入 | **合并候选中优先处理。** 保留 `(turn_id,call_id)` 唯一、action_hash、原始结果回执及历史 pending 的处理策略；不能自动把不确定记录改成 completed。若近期要复用通用外部写账本，则需重新评估是否仍拆分 |
| `business.conversations` + `business.grants` | 新会话创建时一对一，可把 grant_revision/scopes 放入 conversations，少一张表及一次初始化写入 | 会话锁负责提交互斥，grant 锁负责授权变更与执行；合并会让授权检查与同会话受理争同一行。保留 grant_changes 回执。优先级低 |
| `business.turns` + `business.turn_policies` | 一对一扩展，物理上可以合并；读取 Turn 时可以一并带出授权快照和计数 | 授权快照必须保留，不能改为读当前 grant；预算扣减会更新 Turn 热行。先比较 JOIN/批量查询与合表的收益，优先级低 |
| `memory.sources` + `memory.recalls` | 每 Turn 最多 16 条事实引用，可把 `(fact_id,revision,epoch)` 冻结列表放入 source 的 JSONB 中，合并 source 插入和 freeze | 会失去逐项 FK，必须继续核对事实当前 revision/epoch/active；重写遗忘擦除、备份残留检查。当前自动提取只有五类 key，通常远少于 16 条。收益未量化，暂不优先 |
| `memory.forget_requests` + `memory.forget_applied` | 字段基本相同，可统一为 prepared/applied 状态的 forget_events，将记录已应用与删除请求合成一次状态更新 | 必须保留每 owner 一个 prepared、event_id 唯一、每 owner/from_epoch 唯一、持久屏障及幂等重放。低频路径，收益小而恢复回归成本高，建议后置 |

报告合并不是把 `reports` 当作无用表删除：即使没有独立读取 API，它仍承载当前 `create_report` 工具的真实本地写效果。当前三次写在同一事务内，合并后也必须保持失败回滚与重复调用返回相同结果。

`cancel_requests`、`grant_changes`、`admission_changes`、`memory.operations` 都有幂等回执的共同点，可以统一代码抽象；但资源作用域、主键、响应结构及历史重放语义不同。当前不建议为了少几张表，立即揉成一个带大 JSON 的通用 operation_receipts 表。

## 3. 应保留的独立边界

| 表/组合 | 保留原因 |
| --- | --- |
| `turns` / `commands` / `attempts` | 分别承载业务状态、租约与调度、每次执行尝试的封存回执。一个 Command 可有多个 Attempt；Turn waiting 时 Command 可以 done。不能用一个当前状态覆盖执行历史 |
| `agent_runtime.states` / 业务表 | AgentScope 检查点具有 CAS 与墓碑；保存状态、封存执行结果、提交业务完成是三个不同边界 |
| `journal` / `turns` | 消息正文确有重复，但 journal 还提供稳定递增游标和用户/助手消息顺序。直接用 message/result 替代会改变分页与完成顺序，不能先删后补 |
| `events` / `turns` | events 保存逐 revision 的业务历史与 SSE 重连游标；Turn 仅保存当前值。实时进度改用 AgentScope 事件不意味着持久业务事件也应删除 |
| `grants` / `grant_changes`；`admission` / `admission_changes` | 当前状态与不可变的幂等结果不同。把回执揉入当前行会使旧请求重放返回新状态 |
| `interactions` / `attempts` | 审批/输入具有独立 revision、有效期、action hash 和 checkpoint 绑定，一个 attempt 可以有多条交互 |
| `capability_steps` / `tool_calls` | 确定性步骤缓存与可能不确定的写效果回执语义不同；不能统一成无区别的工具结果表 |
| `artifacts` / `states` / `reports` | 分别是独立授权的大对象归档、运行快照、本地发布效果，访问契约与生命周期不同 |
| `memory.owners` / `memory.user_profiles` | 技术上一对一，但 owners 是高频加锁的隐私/版本窄行；画像是低频读取的 Markdown。更重要的是受限 Worker 无画像表权限，合表需重新设计列级权限。**当前保留拆分** |
| `memory.sources` / `turns` / `journal` | source.content 的副本支持 Worker 不访问 business schema 即可提取证据，并按 epoch 独立擦除；画像关闭自动提取时 source 不复制正文 |
| `memory.facts` / `memory.projections` / `embedding_profiles` | 权威事实、可重建向量索引、模型指纹各司其职；同一事实可以有多个 model_revision，索引重建不应改动事实 |
| `memory.operations` / `memory.audit` | 前者是唯一键回执，后者是追加事件；forget/purge_complete 审计还参与恢复证明 |
| `memory.jobs` / `business.commands` | 独立租约、重试、工作类型及受限账号；直接合表会打破 Worker 权限边界 |
| `channel.link_codes` / `bindings` | 一次性短期认领凭证与长期身份/会话映射的基数和生命周期不同 |
| `channel.inbox` / `notices` | 入站事件去重与出站快照/发送不确定状态不同，不是一对一 |
| `memory.privacy_ledger_binding` / 独立 `privacy_ledger.lineages`、`forget_facts` | 主库绑定、恢复 lineage 与独立遗忘事实共同防止旧主库备份复活数据。**独立账本不能合回主库** |

这里没有 `task_snapshots` 表：运行检查点是 `agent_runtime.states`；`business.projections` 是前述本地终态镜像，不能混为一谈。

## 4. 字段重复与查询次数要分别处理

- `business.events.kind/text/tool` 是旧数据库流式进度列；当前持久插入只写 turn/revision/status/reason，进度从 TaskEventHub 内存发布。确认旧记录保留策略后，可以将查询中的 kind/text/tool 改为常量别名，再退役旧列。主要收益是语义清晰，不是数据库往返减少。
- embedding 模型的 model/dimensions 以及 projection.dimensions 包含可从指纹或固定 1024 约束推导的元数据；当前 value 很小，保留诊断用途也合理，不是热点优先项。
- 不应仅因当前没有生产 SELECT 就删除 created_at/updated_at 等运维和保留策略元数据。

与合表相比，以下 SQL 合并更直接降低调用量（均为源码路径计数，未做压测）：

| 调用路径 | 当前 | 建议 |
| --- | --- | --- |
| [MemoryProcessingService.claim](../../../src/main/java/com/financeclaw/app/memory/MemoryProcessingService.java) | next → claim → job，有任务时 3 次 SQL | CTE 锁定候选并 UPDATE RETURNING，合成 1 次；保留租约及 SKIP LOCKED |
| 同服务 live | job 加锁查询 + liveLease，2 次 SQL | 一次锁定查询返回 job 与数据库时钟计算的租约有效性，提交时仍检查 token/epoch |
| GrantService 幂等重放 | 先取 hash，再取原回执 | 一次查询返回 hash 与原结果，保留先重放、再检查当前 revision 的顺序 |
| FeishuDelivery 空闲轮询 | enabled 时每轮 recover + dirty + pending；默认每秒一轮 | 采用有上限的空闲退避、任务提示和必要恢复扫描；不可自动重投 unknown 发送 |

## 5. Mapper 命名参考与项目约定

已核对阿里官方 [Java Coding Guidelines — Naming Conventions](https://alibaba.github.io/Alibaba-Java-Coding-Guidelines/#naming-conventions) 第 15 条。手册将分层命名列为 **For Reference / 参考**，并非全部是强制规则：单对象 get、集合 list、统计 count、插入 save/insert、删除 remove/delete、修改 update。中文黄山版由 [alibaba/p3c](https://github.com/alibaba/p3c) 官方仓库提供。

本项目的 Mapper 建议选用 `insert/update/delete` 与 SQL 类型对齐；聚合结果是一个多字段统计对象时使用 `get…Statistics/Summary`，单个数值统计才使用 `count…`。不要仅看 MyBatis 注解名称：`@Select(affectData=true)` 也能承载 INSERT/UPDATE RETURNING，应按实际 SQL 副作用命名。

以下属于**项目补充约定**，不宣称是手册原文：

1. 布尔存在判断使用 `exists…`，权限或能力判断使用 `is…`，保留语义明确的状态值查询。
2. 锁查询体现 `ForUpdate`、`ForShare`，队列选取需要时体现 `SkipLocked`。
3. 乐观写体现 `IfVersion/ByExpectedVersion/ByRevision`；去重插入体现 `IfAbsent`。
4. UPSERT、逻辑擦除、物理删除分开命名；返回值及影响行数语义写入 Javadoc。
5. 应用层 `admit/forget/claim/report` 是业务动作，Gateway/Service 不必为了 Mapper 命名一起改成 CRUD。

### 优先修改的实际歧义

| 当前方法 | 建议方法 | 原因 |
| --- | --- | --- |
| `TaskMapper.turn` / `events` | `getTurnById` / `listTurnEventsAfterRevision` | 单对象、集合和游标语义明确 |
| `TaskMapper.event` | `insertTurnEventAndReturn` | 实际插入事件，当前名称容易被误认为读取 |
| `TaskMapper.renew` | `updateCommandLeaseAndGetCancellation` | 更新租约，nullable Boolean 还编码了取消状态 |
| `TaskMapper.projectBatch` | `updateOutboxDeliveryBatchWithProjection`（若链路保留） | 同时写投影和确认 outbox，返回的是 outbox 确认行数；若按本报告退役，无需先做无用重命名 |
| `TaskMapper.messages` / `journal` | `listJournalEntriesAfterSequence` / `insertJournalEntryIfAbsent` | 区分消息查询与插入 |
| `MemoryMapper.profile` / `userProfile` | `getEmbeddingProfileFingerprint` / `getUserProfile` | 前者是 embedding 指纹，后者才是手工画像 |
| `MemoryMapper.purge` / `purged` | `getNextPurgeOwnerForUpdateSkipLocked` / `updateOwnerPurgeCompleted` | 前者只取出并锁定候选，后者才修改状态 |
| `MemoryMapper.eraseFacts` | `updateFactsForgotten` | 执行 UPDATE 擦除正文、推进 revision，不是 DELETE；sources/jobs 同理 |
| `RuntimeMapper.findState` / `compareAndSetState` | `getState` / `updateStateByExpectedVersion` | 统一单对象查询及 CAS 写入语义；已有 CAS 名本身语义清楚，优先级低于短歧义名 |
| `RuntimeMapper.tombstoneState` | `insertOrUpdateStateTombstone` | 是保留版本的墓碑 UPSERT，不是物理删行 |
| `FeishuMapper.find` / `notice` | `getBindingByChannelIdentityForUpdate` / `getNoticeForUpdate` | 当前查询实际持有行锁 |
| `OperationsMapper.tasks` / `drain` | `getTaskStatistics` / `getDrainSummary` | 返回多字段聚合对象，不是任务列表 |
| `admissionReady/artifactsReady/stateReady` | `is…TableReadable` | SQL 用于读取权限探针，不是检查是否存在业务数据 |

`renew` 不能在重命名时顺便改成 primitive boolean：当前 `null` 表示续期失败，`false` 表示成功且未取消，`true` 表示成功且取消中。[ExecutionLeaseService](../../../src/main/java/com/financeclaw/app/execution/ExecutionLeaseService.java) 已正确区分。`eraseSources/eraseFacts/eraseJobs` 也不能简单批量替换成 delete；事务内的 CAS、租约、RETURNING、flushCache 和 affected rows 契约都应保持。

已盘点 6 个生产 Mapper 的全部 **147 个方法**，逐项建议见 [Mapper 命名映射清单](../../architecture/evidence/mapper-naming-review.json)。这是候选清单，不表示每个现有名称都有缺陷，更不是批量替换脚本。

| Mapper | 方法数 |
| --- | ---: |
| TaskMapper | 40 |
| GovernanceMapper | 32 |
| MemoryMapper | 43 |
| FeishuMapper | 19 |
| OperationsMapper | 9 |
| RuntimeMapper | 4 |

后续实施可为 Mapper 单独增加名称检查，允许上述 RETURNING/UPSERT 等例外；不对 DTO getter、Domain 行为或所有 Service 方法套一条全局正则。

## 6. 建议实施顺序与验证边界

1. 优先退出两张历史表及本地投影链路；保留旧 Flyway 文件，通过后续迁移实施。先解决不再需要的运行成本。
2. 对保留的 Mapper 按文件做纯命名调整，同步调用者、Mockito、反射/方法引用、MyBatis statement ID 和测试；不同时修改 SQL 行为。
3. 合并 Memory Worker claim/live 查询、grant 回执查询，补飞书空闲退避；通过实际 SQL 计数与并发验证确认收益。
4. 评估 reports/tool_calls 合并。其余表按真实容量和锁等待证据再决定。

实际删表之前需验证旧版本升级、存量回执、归档、排空契约、恢复及角色权限；仅改 Mapper 名称也需完整编译与真实 MyBatis 调用覆盖，编译成功不能证明字符串 statement ID 没有遗留。

原审查批次只新增文档及候选映射；以下记录后续实施，原审查并非迁移验收证据。


## 7. 已实施的修改

- **V10** 删除 `skill_refs`、M0 `tool_effects`、本地 `outbox/projections`，删除空转投影服务和定时器；旧 Flyway 文件保持原样。M0 实验账本由测试资源单独创建。排空 API 的 `pending_outbox` 保留兼容字段并固定为 0。升级前归档与停旧版本说明见[新库初始化说明](../../architecture/fresh-schema.md)。
- **V11** 将 `tool_calls` 回执合入 `reports`，保留原报告 ID、标题及回执原文字节；receipt-only 不编造实体，report-only 标记 `legacy_untracked`，pending 或结果缺失继续阻断。新发布仅一条 INSERT。目标主库应用表从 39 减为 **34**，独立账本仍为 2 张。
- Mapper 采用类型解析后改名，避免替换到 Service/Domain 的同名业务方法；调用点、方法引用及测试同步调整。租约三态、CAS、逻辑擦除与 UPSERT 的 SQL 行为保留。原 147 项候选中的退役方法直接删除。
- Memory Worker 有任务时 claim 从 3 次 SQL 减为 1 次，租约有效性校验从 2 次减为 1 次；保留 SKIP LOCKED、token/owner/epoch/revision 校验与锁后数据库时间判断。
- 已存在用户的受理事务使用隐私行共享锁，允许独立会话并行受理；画像修改和遗忘仍持独占锁，等待受理事务结束。首次建立用户行仍可能因唯一键串行。
- 授权变更的幂等回放合并读取请求 hash 与原回执，仍先处理旧请求重放，再检查当前 grant revision。
- 飞书 scheduled 空闲轮询退避至 5 秒，恢复扫描独立每 5 秒；空闲稳态路径约从 3 SQL/s 减为 0.6 SQL/s。unknown 或缺少可靠回执的发送不自动重投。以上是路径计数，不是压测结果。

新增 V9→V10、V10→V11 升级/回滚测试、Memory Worker 竞争/租约过期测试与飞书退避测试。未对本机业务数据库执行迁移；数据库及部署验证将在隔离测试环境完成。编译、测试和远程 CI 结果在本轮交付时记录。
