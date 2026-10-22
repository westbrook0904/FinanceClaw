# 数据库访问与实时事件优化

核对日期：2026-10-22。本文描述当前工作区相对于本轮修改前 `2407f30` 的数据库访问变化；部署入口见[同进程运行与事件推送](combined-runtime.md)。

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
| 执行 heartbeat | 每个活动执行 3 次；默认 2 秒一次，即约 `1.5 × 活动执行数` 次/秒 | command fence、续租、取消状态都需 authoritative state；可评估条件更新加 `RETURNING`，保留失效 fencing 和取消响应 |
| Worker 空闲调度 | 每轮 expired 检查和 next-command 查询共 2 次；默认 250ms，约 8 次/秒/启用进程 | 崩溃恢复和持久队列仍由 SQL 负责；可增加本地唤醒与空闲退避，再保留较低频恢复扫描 |
| Outbox 空闲扫描 | 每轮 1 次；默认 250ms，约 4 次/秒/启用进程 | 未投递意图需要重启后恢复；可增加提交后本地唤醒和退避，仍保留补偿扫描 |
| 记忆 Worker 空闲扫描 | purge 与 job 各 1 次；默认 1 秒，约 2 次/秒 | 使用独立受限身份完成提取、索引与隐私清理，可独立评估调度间隔 |
| AgentScope checkpoint | 每次保存保留 command fencing 与版本 CAS | 不能用仅内存保存替代审批恢复、执行版本和提交证据 |
| 业务结果与外部效果 | 回执、授权复核、状态转换、journal/outbox 等短事务 | 保存成功或流结束均不等于业务完成；不可因减少查询而删除这些边界 |

以上周期速率忽略查询执行耗时，表示配置周期下的近似调用频率。调度器的延迟、业务处理耗时及是否启用对应角色都会影响真实速率，不能与并发用户数直接画等号。

此外，[MarketResearchService](../../src/main/java/com/financeclaw/app/research/MarketResearchService.java) 的 `begin`、`complete` 与 [CapabilityStepService](../../src/main/java/com/financeclaw/app/execution/CapabilityStepService.java) 仍会分别复核治理状态。后续可在同一事务内复用已锁定的 Policy、合并 step 查询与回执操作；需要先验证工具预算、子任务重放及授权撤销，不能跨事务缓存授权结果。首次受理、审批、取消与完成路径的固定 SQL 也仍存在，本轮优先消除了随连接、文本增量和历史条数放大的访问。

## 验证范围

- [MemoryHistoryQueryTest](../../src/test/java/com/financeclaw/architecture/MemoryHistoryQueryTest.java)：8 条历史只使用一个 owner 锁和一次批量读取；覆盖原顺序、跨 owner、purge、下一请求重新读取 epoch，以及已存在/新 owner 的访问路径。
- [ToolDispatchQueryTest](../../src/test/java/com/financeclaw/architecture/ToolDispatchQueryTest.java)：一次锁内 Policy 读取与预算更新；覆盖租约失效、scope 拒绝、grant 撤销、隐私变化、不确定效果、预算耗尽，以及市场读取的预算和错误兼容；正常进度不访问数据库，拒绝的进度只复核一次治理原因且不消耗预算。
- [TaskEventsTest](../../src/test/java/com/financeclaw/architecture/TaskEventsTest.java)：空闲零周期 SQL、分页回放、实时增量无 ID、乱序缺口补读、终态关闭及鉴权/角色拒绝。
- [TaskEventHubTest](../../src/test/java/com/financeclaw/architecture/TaskEventHubTest.java)：活动 attempt、身份隔离、失效时丢弃旧内容、提交后发布、有界缓冲和慢连接隔离。
- [MemoryIT](../../src/test/java/com/financeclaw/m4/MemoryIT.java)：真实 PostgreSQL 批量 source 查询、owner/epoch 过滤、遗忘后过滤；需要可用 Docker Engine。
- [TaskLifecycleIT](../../src/test/java/com/financeclaw/m1/TaskLifecycleIT.java)：HTTP SSE 重连、身份隔离、任务完成与恢复行为；需要真实数据库。

单元测试的调用次数断言不替代 JDBC/数据库层观测。部署验收应另行记录连接数、连接池等待、每请求 SQL 数、数据库事务/锁等待、SSE 缓冲溢出、模型与工具耗时，以及多 owner、多会话下的响应分位数。本文不预先声明这些指标已经达标。

## 本次验证结果

2026-10-22 最终代码的一次完整 `verify spotless:check` 已通过：950 项单元/HTTP 契约/架构测试、275 项集成测试，失败、错误、跳过均为 0，打包与格式检查通过。该批在本地运行模拟模型、MCP、embedding 和飞书服务，数据库与打包实验使用真实 PostgreSQL/Docker。

同一最终产物的隔离 Compose 验收通过 28 项，覆盖合并启动、12 会话/4 并发的离线样本、受理控制、SIGKILL 后审批恢复、数据库中断、受限记忆、备份恢复及旧隐私快照拦截；不代表生产容量。静态交付检查也通过。详细记录见[验证汇总](evidence/native-events-db-verification.json)与[容器证据](evidence/combined-runtime-compose.json)。
