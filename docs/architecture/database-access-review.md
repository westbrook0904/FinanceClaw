# 数据库访问与实时事件优化

核对日期：2026-10-23。首批事件与查询优化相对于 `2407f30`，后续 Worker、Outbox 与心跳优化相对于首批检查点 `ae16c36`；部署入口见[同进程运行与事件推送](combined-runtime.md)。

V10 后续调整：当前已退役没有业务读取方的本地 Outbox/Projection 链路，下文该链路的批量优化和验证保留为历史记录；相关生产代码及专项测试已删除。当前空闲 Command 扫描与恢复合计约 0.7 次/秒/启用进程（不含健康检查、记忆或渠道），不再产生 Outbox 轮询。升级和归档要求见[表退役操作](../../ops/retire-legacy-tables.md)。

这里的次数是沿调用链统计的 Mapper/SQL 入口调用数，以及针对调用次数、权限和隐私边界设计的单元测试约束。它们不包括事务开启/提交、连接建立、健康检查、数据库内部工作或查询执行时间；当前 MyBatis 使用 `STATEMENT` 本地缓存范围，不依赖跨 Mapper 调用的缓存来省略检查。本文不是并发压测报告，不能据此宣称生产 QPS、P99、最大用户数或容量提升比例。实际通过的测试批次以本次执行日志和交付说明为准。

## 已消除的放大路径

以下执行检查默认记忆功能开启、owner 已由受理建立，工具具有需要核对的 scope；不计工具自身的业务 SQL。

| 路径 | 修改前 | 当前 | 变化原因 |
| --- | ---: | ---: | --- |
| SSE 建立前的归属/游标检查 | 3 | 1 | 合并 owner、conversation、turn 读取；历史回放单独执行 |
| 每个空闲 SSE 连接 | 每 250ms 5 次，约 20 次/秒 | 0 次周期查询 | AgentScope 事件直接推送，保活帧只使用内存计时器 |
| SSE 历史回放 | 每轮轮询重复检查归属并读事件 | 每页 1 次 | 每页最多 100 条，复用已授权的不可变任务身份 |
| 每个正常实时文本增量/工具进度事件 | 10 | 0 | 事件不再逐条更新 revision 或写入事件表；已失效的异常路径单独核对原因 |
| 8 条历史的隐私过滤 | 24 | 最多 2 | 单次 owner 锁加批量 source ID 查询 |
| 一次普通工具 dispatch 的授权与预算 | 18 | 8 | scope 校验与预算预留复用同一事务中的 Policy |
| `MarketDataService.read` 的授权与预算 | 18 | 8 | 使用相同的合并入口，保留原有预算次数 |
| 已存在 owner 的一次隐私检查 | 3 | 2 | 不再每次执行 `INSERT ... ON CONFLICT DO NOTHING` |
| `GovernedTools` 初始化读取 scopes | 2 | 1 | 去掉构造链中的重复读取 |

记忆关闭时，普通工具 dispatch 从 12 次降到 6 次，实时进度从 7 次降到 0 次，历史隐私过滤仍为 0 次。新 owner 第一次初始化需要“查询、插入、再锁定”3 次，而原先为2次；后续所有使用同一 owner 的请求省去重复插入。

### SSE 的具体计数与恢复行为

旧 [TaskEvents](../../src/main/java/com/financeclaw/api/TaskEvents.java) 在建立连接时先调用一次 `tasks.events`：查 conversation 归属、查 turn、查 events，共 3 次。订阅后每轮又调用 `tasks.events` 的 3 次和 `tasks.get` 的 2 次，共 5 次。默认 250ms 周期即每连接约 20 次查询/秒，即使没有新事件也如此；计时器的首轮立即执行，同样会产生这 5 次。

当前 [TaskQueryService](../../src/main/java/com/financeclaw/app/task/TaskQueryService.java) 使用 `ownedTurn` 在一次查询中核对 owner、conversation 和 turn。TaskEvents 随后先订阅 [TaskEventHub](../../src/main/java/com/financeclaw/app/task/TaskEventHub.java)，再从 SQL 回放持久事件，之后消费实时通知。常见的单页回放连接只需 1 次归属查询加 1 次历史查询。历史页恰好满 100 条时，需要继续查询下一页，可能包含一次确认结束的空页；检测到持久 revision 缺口时也会按需补读，均不属于定时轮询。

当前支持的实时推送拓扑是一个 `standalone` 业务 JVM。Hub 是进程内通道，跨进程事件不会自动传播；拆分 API 角色的 SSE 返回 `sse_requires_standalone`。不能把“空闲 SSE 零查询”理解成已经具备跨实例消息总线。

### 为什么每个文本增量原先会产生 10 次 SQL

旧 [ExecutionPolicyService.progress](../../src/main/java/com/financeclaw/app/execution/ExecutionPolicyService.java) 每次依次执行：

1. 锁 command，验证 attempt、fence、租约：1 次。
2. 锁 turn，确认仍为 running：1 次。
3. 记忆隐私校验：ensure owner、锁 owner、读 source，共 3 次。
4. 读取 Policy、锁 grant、检查 uncertain effect：3 次。
5. 增加 turn revision、写入 progress event：2 次。

合计 10 次；记忆关闭后省去第 3 项，为 7 次。这是每个 `TextBlockDeltaEvent` 的开销，不能将其直接等同于 tokenizer 的每一个 token：一次增量可能包含多个字符或 token。工具开始/结束等走同一路径的进度事件也有相同开销。

当前 [TaskAgentRuntime](../../src/main/java/com/financeclaw/infrastructure/agentscope/execution/TaskAgentRuntime.java) 继续消费 AgentScope 原生 `streamEvents`，将公开事件交给 Hub；正常 `progress` 不访问数据库。每次执行注册独立的活动 attempt，取消、权限和隐私失效会撤销本地进度发布并过滤 Hub 缓冲中的旧内容；已经交给 HTTP 的帧无法撤回。Hub 已拒绝发布并抛出 `Fenced` 时，异常路径调用一次 `checkDispatch`，从数据库恢复具体的 `authorization_changed` 或 `memory_privacy_changed` 原因；取消、租约失效或已关闭的本地 handle 仍保持 fenced。这里的“一次”是一次完整治理校验，最多 7 个 Mapper 调用（记忆开启、owner 已存在），不是一条 SQL；失败的阶段可能更早终止。这不会对正常增量增加查询。模型/工具实际调用、状态保存及最终提交仍经过原来的数据库治理边界。

实时增量没有持久 SSE ID，不作为断线恢复的精确逐 token 日志。持久业务状态仍在事务内写入，提交后才发布通知，SSE ID 只对应持久 revision；重连回放业务状态事件；完整结果通过任务详情或会话消息接口读取。每个观察者使用有界缓冲，慢连接溢出会被关闭，不反向阻塞模型执行，也不会重新执行任务。

### 历史与工具调用

[TaskMapper.history](../../src/main/java/com/financeclaw/infrastructure/task/TaskMapper.java) 当前最多返回 8 条已完成历史。旧实现对每条调用 `sameEpoch`，分别 ensure owner、锁 owner、读 source，因此是 `3 × N`，最多 24 次。当前 [MemoryService.filterHistory](../../src/main/java/com/financeclaw/app/memory/MemoryService.java) 锁定一个 owner 后，使用[批量 source 查询](../../src/main/java/com/financeclaw/infrastructure/memory/MemoryMapper.java)按 tenant、subject、epoch 和 ID 集合筛选。查询只返回 ID，不重复读取 source 正文；输出保留原历史顺序，purge pending 时不返回历史，没有跨请求缓存隐私状态。

旧普通工具调用依次执行 `checkDispatch` 8 次、`scopes` 1 次、`consume` 9 次，共 18 次。当前 `consumeTool` 在一个 fenced 事务内完成 command 1 次、turn 1 次、隐私 2 次、Policy/grant/uncertain 3 次、预算更新 1 次，共 8 次。权限撤销、租约失效、记忆遗忘、未核对外部效果和预算耗尽仍在执行工具之前拒绝。

[GovernedTools](../../src/main/java/com/financeclaw/infrastructure/agentscope/execution/GovernedTools.java) 的普通工具与技能调用，以及 [MarketDataService](../../src/main/java/com/financeclaw/app/market/MarketDataService.java) 都使用该入口。市场读取缺少权限仍返回原 `market_denied`。表格分别统计工具包装层和市场服务内部检查；当一次调用同时经过两层时，两个预算入口仍各自执行，不能把两行的改进误认为整条业务链只剩 8 次 SQL。本轮未改变既有预算计数含义。

## 保留的数据库工作

| 路径 | 当前固定开销 | 保留原因与可继续改善的方向 |
| --- | --- | --- |
| 执行 heartbeat | 每个活动执行从 3 次降为 1 次；默认 2 秒一次，即约 `0.5 × 活动执行数` 次/秒 | 单条条件更新同时返回取消状态；保留 command、Turn 顺序锁定和等待后的过期检查 |
| Worker 空闲调度 | 稳定空闲时队列约 0.2 次/秒，独立恢复扫描约 0.5 次/秒；原合计约 8 次/秒 | 提交后本地唤醒，空查退避至 5 秒；恢复扫描独立每 2 秒，满载时也执行 |
| Outbox 扫描与投影 | 稳定空闲约 0.2 次/秒，原约 4 次/秒；50 条意图从 150 条 SQL / 50 个事务降为 1 条 SQL / 1 个事务 | 提交后唤醒、最多 5 秒兜底扫描；每批最多 50 条，投影与确认同事务 |
| 记忆 Worker 空闲扫描 | purge 与 job 各 1 次；默认 1 秒，约 2 次/秒 | 使用独立受限身份完成提取、索引与隐私清理，可独立评估调度间隔 |
| AgentScope checkpoint | 每次保存保留 command fencing 与版本 CAS | 不能用仅内存保存替代审批恢复、执行版本和提交证据 |
| 业务结果与外部效果 | 回执、授权复核、状态转换、journal/outbox 等短事务 | 保存成功或流结束均不等于业务完成；不可因减少查询而删除这些边界 |

以上周期速率忽略查询执行耗时，表示配置周期下的近似调用频率。调度器的延迟、业务处理耗时及是否启用对应角色都会影响真实速率，不能与并发用户数直接画等号。

### Worker 与 Outbox 的本地唤醒和兜底扫描

[WorkSignals](../../src/main/java/com/financeclaw/app/task/WorkSignals.java) 对 Command、Outbox 各保留一个内存计数器。新任务受理、审批回答后重新排队、新 Outbox 意图写入成功时，在事务提交后递增相应计数器；回滚和幂等重放不通知。这里没有额外 SQL，也不在提交线程执行任务。轮询仍以数据库为准，进程重启或外部进程写入都由兜底扫描发现。

[TaskWorker](../../src/main/java/com/financeclaw/worker/TaskWorker.java) 和 原 OutboxProjector 默认每 250ms 执行一次本地调度检查。连续空查按 250、500、1000、2000、4000、5000ms 退避，只有到期或收到通知才访问数据库；新提交在下一个可执行的本地 tick 复扫。扫描开始前捕获通知代次，避免查询返回空到记录退避之间发生提交时丢失唤醒。执行槽位释放同样能唤醒 Worker；满载、关闭或禁用时不查待执行队列，查询失败也退避。

恢复扫描从 Worker claim 循环分离，默认每 2 秒执行，不受空闲退避或槽位耗尽影响。稳定空闲时，队列、恢复、Outbox 三处合计从约 12 次/秒降到约 0.9 次/秒/启用进程；这是配置和调用路径推算，不含启动退避爬升、健康检查、记忆 Worker、处理任务和 SQL 执行时间。外部进程提交不会触发本地通知，需要等待最多 5 秒的退避间隔及调度/数据库耗时。实时 SSE 仍只支持单个 standalone 业务 JVM。

[批量投影](../../src/main/java/com/financeclaw/infrastructure/task/TaskMapper.java) 用一条 CTE 领取最多 50 条 `FOR UPDATE SKIP LOCKED` 意图，按 Turn 取最高 revision、稳定顺序 UPSERT，然后确认这些意图。同批去重和保留已有较高 revision 防止旧状态覆盖新投影；UPSERT 直接返回有效 revision，避免并发提交后的快照遗漏。任何投影或确认错误均整批回滚。50 条记录减少的是往返与事务数，每个意图仍需持久确认；[V8 部分索引](../../src/main/resources/db/migration/V8__outbox_pending_index.sql) 只索引未确认记录。升级时由 migrate 先执行 V8；现有大表建索引仍需按部署维护窗口评估。

### 心跳的一次往返与锁语义

[ExecutionLeaseService.heartbeat](../../src/main/java/com/financeclaw/app/execution/ExecutionLeaseService.java) 保留短事务，但不再分别执行锁 command、续租、查 Turn 三条语句。[TaskMapper.renew](../../src/main/java/com/financeclaw/infrastructure/task/TaskMapper.java) 使用两个有依赖关系的 `MATERIALIZED` CTE，先锁 command，再锁对应 Turn，最后 `UPDATE ... RETURNING` 返回是否为 `cancelling`。`Boolean` 的 `null` 表示没有匹配的可续租命令，服务将其转为 `Fenced`；`false` 表示续租成功且未取消，二者不能混淆。

命令必须匹配原 attempt、fence、Turn 身份并仍为 running。租约到期判断依赖已取得两行锁的 CTE 输出，使用 `clock_timestamp()`，避免把锁等待前尚有效、等待后已经过期的租约重新续活。Turn 使用锁定读取，是为了在语句等待取消事务释放 command 后取得最新取消状态；普通子查询可能仍读取该语句开始时的旧快照。代价是每次心跳也会短暂锁 Turn，减少的是 SQL 往返，并非取消行锁竞争。完成、换代或取消使用相同的 command→Turn 顺序，因此不会为了减少查询而改变锁顺序。

[专用心跳调度器](../../src/main/java/com/financeclaw/bootstrap/ExecutionSchedulingConfiguration.java) 使用独立的一条线程；常规定时任务保留默认两条线程。显式保留 Spring 的 `applicationTaskExecutor`（8 线程、队列 256），避免自定义 scheduler 让 MVC/SSE 写出回退到无界默认执行器。Outbox/恢复/飞书处理占用默认线程时不会排队阻塞心跳调度。每次 tick 将活动执行交给独立心跳执行池，并发上限与执行槽位一致、队列容量为 0。同一 attempt 只允许一个续租在途，慢续租不会堆积历史心跳，也不串行拖住其他执行；线程满时留给下一次 tick 重试，关闭时停止提交并尝试中断在途任务；已进入 JDBC 的续租仍可能在 SQL/连接超时边界内完成，不能把 Java 线程中断当成事务已经回滚。业务连接池仍共享，连接池耗尽或数据库长锁等待仍可能导致租约失效，本轮未隔离这些资源，也不会因此复活过期租约。

此外，[MarketResearchService](../../src/main/java/com/financeclaw/app/research/MarketResearchService.java) 的 `begin`、`complete` 与 [CapabilityStepService](../../src/main/java/com/financeclaw/app/execution/CapabilityStepService.java) 仍会分别复核治理状态。后续可在同一事务内复用已锁定的 Policy、合并 step 查询与回执操作；需要先验证工具预算、子任务重放及授权撤销，不能跨事务缓存授权结果。首次受理、审批、取消与完成路径的固定 SQL 也仍存在，前一轮消除了随连接、文本增量和历史条数放大的访问，本轮继续减少后台空转与固定续租往返。

## 验证范围

- [MemoryHistoryQueryTest](../../src/test/java/com/financeclaw/architecture/MemoryHistoryQueryTest.java)：8 条历史只使用一个 owner 锁和一次批量读取；覆盖原顺序、跨 owner、purge、下一请求重新读取 epoch，以及已存在/新 owner 的访问路径。
- [ToolDispatchQueryTest](../../src/test/java/com/financeclaw/architecture/ToolDispatchQueryTest.java)：一次锁内 Policy 读取与预算更新；覆盖租约失效、scope 拒绝、grant 撤销、隐私变化、不确定效果、预算耗尽，以及市场读取的预算和错误兼容；正常进度不访问数据库，拒绝的进度只复核一次治理原因且不消耗预算。
- [TaskEventsTest](../../src/test/java/com/financeclaw/architecture/TaskEventsTest.java)：空闲零周期 SQL、分页回放、实时增量无 ID、乱序缺口补读、终态关闭及鉴权/角色拒绝。
- [TaskEventHubTest](../../src/test/java/com/financeclaw/architecture/TaskEventHubTest.java)：活动 attempt、身份隔离、失效时丢弃旧内容、提交后发布、有界缓冲和慢连接隔离。
- [MemoryIT](../../src/test/java/com/financeclaw/m4/MemoryIT.java)：真实 PostgreSQL 批量 source 查询、owner/epoch 过滤、遗忘后过滤；需要可用 Docker Engine。
- [TaskLifecycleIT](../../src/test/java/com/financeclaw/m1/TaskLifecycleIT.java)：HTTP SSE 重连、身份隔离、任务完成与恢复行为；需要真实数据库。
- [ExecutionHeartbeatTest](../../src/test/java/com/financeclaw/architecture/ExecutionHeartbeatTest.java)：一次心跳只调用一个 Mapper 语句，成功的 `false` 与没有匹配行区分，失败不追加查询。
- [ExecutionHeartbeatIT](../../src/test/java/com/financeclaw/m1/ExecutionHeartbeatIT.java)：真实 PostgreSQL 取消/完成/换代竞争、等待 command 或 Turn 锁期间过期不续租、command 先锁的冲突探针，以及事务回滚；需要可用 Docker Engine。

- [IdlePollTest](../../src/test/java/com/financeclaw/worker/IdlePollTest.java)、[TaskWorkerPollingTest](../../src/test/java/com/financeclaw/worker/TaskWorkerPollingTest.java)、原 OutboxProjectorTest（V10 退役）：可控时钟验证退避、提交/容量释放竞态、独立恢复、失败退避与有界批次；一条心跳阻塞时其他执行仍续租，同一 attempt 不重入。
- [WorkSignalsTest](../../src/test/java/com/financeclaw/architecture/WorkSignalsTest.java) 与生命周期、审批集成测试：事务提交后通知、回滚不通知、幂等重放不重复通知。
- 原 OutboxProjectionIT（V10 退役）：真实 PostgreSQL 验证 50 条单语句、4 消费者 125 条记录、版本不回退、锁竞争和整批回滚。
- [SchedulingConfigurationTest](../../src/test/java/com/financeclaw/bootstrap/SchedulingConfigurationTest.java)：占满默认调度器时，具名心跳定时任务仍在独立线程运行；加载实际 YAML，验证 HTTP 异步线程池仍被创建且有界。

单元测试的调用次数断言不替代 JDBC/数据库层观测。部署验收应另行记录连接数、连接池等待、每请求 SQL 数、数据库事务/锁等待、SSE 缓冲溢出、模型与工具耗时，以及多 owner、多会话下的响应分位数。本文不预先声明这些指标已经达标。

## 首批检查点验证结果

2026-10-22 首批检查点（后提交为 `ae16c36`）的一次完整 `verify spotless:check` 已通过：950 项单元/HTTP 契约/架构测试、275 项集成测试，失败、错误、跳过均为 0，打包与格式检查通过。该批在本地运行模拟模型、MCP、embedding 和飞书服务，数据库与打包实验使用真实 PostgreSQL/Docker。

同一最终产物的隔离 Compose 验收通过 28 项，覆盖合并启动、12 会话/4 并发的离线样本、受理控制、SIGKILL 后审批恢复、数据库中断、受限记忆、备份恢复及旧隐私快照拦截；不代表生产容量。静态交付检查也通过。详细记录见[验证汇总](evidence/native-events-db-verification.json)与[容器证据](evidence/combined-runtime-compose.json)。

## 后台访问优化验证结果

2026-10-23 对本轮生产代码执行完整 Maven 回归。角色装配测试最初仍要求 V7，并使用拆分恢复调度之前的任务数；更新测试为 V8、逐个核对各角色允许的定时任务以及专用心跳调度器后，该组 10 项定向复测通过。另调整心跳测试的收尾同步并复跑 9 项，避免夹具关闭时产生额外中断日志。这些后续修改只涉及测试，生产源码与打包 JAR 均未变化。

最终报告覆盖 981 项单元/HTTP 契约/架构测试和 298 项集成测试，失败、错误、跳过均为 0；最终定向复测的 Spotless 检查通过。这里是完整批次加受影响测试复跑的汇总，不宣称首次 `verify` 一次全绿。相同 JAR 的隔离 Compose 验收通过 28 项，包含 V8 启动、离线并发、暂停受理、SIGKILL 审批恢复、数据库中断、受限记忆和隐私备份恢复。

详细命令、初始失败原因、复测结果、源码及 JAR 校验值见[后台优化验证记录](evidence/background-db-verification.json)，容器记录见[后台优化 Compose 证据](evidence/background-db-compose.json)。SQL 次数变化是调用路径和测试约束，不等于生产吞吐或延迟改善比例。
