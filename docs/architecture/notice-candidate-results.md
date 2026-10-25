# 通知候选结果瘦身：P3a.14

**状态：已推送 `e115834`，远程完整 CI 通过。** 前置 `dda1d7d` 已推送，[run 40184268889](https://github.com/westbrook0904/FinanceClaw/actions/runs/40184268889) 整条成功：1,885 Java、46 脚本、44 部署检查通过。该远程结果仅对应 P3a.13；本阶段的 [1,493 项本地选定验收](evidence/p3a14-implementation.json)单独记录。

## 为什么只返回两个标识

原候选查询返回 8 列完整 Notice，但唯一生产调用方只使用 `id`、`binding_id` 定位下一步锁定读取。现在 [FeishuMapper](../../src/main/java/com/financeclaw/infrastructure/channel/FeishuMapper.java) 返回两字段 `NoticeCandidate`，使用明确的构造参数映射；原 statement 名 `getNextPendingNotice`、pending 条件、created_at 排序和 LIMIT 1 保持。

生产只修改 Mapper。[FeishuDelivery](../../src/main/java/com/financeclaw/channel/FeishuDelivery.java) 字节与前置提交相同：先按 binding→notice 锁序读取完整实际通知，再检查状态、静音、activeTurn、binding revision，独立查询当前 owner/Turn 并检查 Turn revision，最后执行 pending→sending CAS。候选仍只用于定位，不能代替锁定行或授权快照；缺失锁定结果时不回退、不写入、不发送。

后续 CTE 的 `notice_payload` 仍含正文，独立 `tasks.get` 的 Turn.result 也仍含正文。本阶段只消除候选发现时的重复映射，不减少 SQL 次数，不改索引、schema、队列顺序、锁序、授权门禁、投影事务或轮询/恢复/心跳频率。

## 如何测量

[NoticeCandidateQueryIT](../../src/test/java/com/financeclaw/p3/NoticeCandidateQueryIT.java) 用真实 PostgreSQL 和正常绑定、受理、离线 Worker 执行、投影及 `/new` 失效路径生成数据，只 mock 最后的 AgentScopeFeishu.send 边界。大输入为 3,000 个 UTF-16 code unit、6,600 UTF-8 字节；预期正文取实际 Turn/Notice，并断言两者相等且未触发 4,000 UTF-16 截断。

[结果观察器](../../src/test/java/com/financeclaw/p3/NoticeResultSetRecorder.java) 与 SQL execute 计数器分开：只匹配两个候选模板和锁定 CTE，读取列元数据，观察生产实际调用的 next/getString，不额外推进游标或读取正文。只记录 getString 已返回正文的 UTF-8 重编码长度，按 ResultSet/行/列去重；空结果仍需记录列元数据。SQL 与结果观察序列逐项对照，不能用漏观测得到零正文。原生 unwrap 句柄和其他 JDBC 使用模式不在本观察器边界内。

[两项协议校准单元测试](../../src/test/java/com/financeclaw/p3/NoticeResultSetRecorderTest.java)覆盖 execute/getResultSet、executeQuery、重复获取结果集、按列名/序号重复读取、空结果、内部 Statement/Connection 包装身份；不把这两项测试扩称为全部 DataSource 关闭行为验证。原始证据仅导出列名、摘要、计数和固定业务布尔值，不导出正文、路由或 owner 值。

## 前后实测

下表正文为候选 getter 返回值的 UTF-8 **逻辑字节**；锁定通知的对应长度逐项保持。

| 测量窗口 | SQL 前→后 | 候选正文逻辑字节前→后 | commit/rollback |
| --- | --- | --- | --- |
| 正常大正文发送 | 6→6 | 6,612→0 | 1/0 |
| 正常发送后重建协调器，空候选 | 2→2 | 0→0 | 0/0 |
| 20 条失效通知前缀 | 61→61 | 132,410→0 | 20/0 |
| 下一 tick 发送另一 owner 的有效通知 | 6→6 | 6,622→0 | 1/0 |
| 积压处理后空候选 | 2→2 | 0→0 | 0/0 |
| 发送异常转为 unknown | 6→6 | 6,620→0 | 1/0 |
| 重建协调器后 unknown 不重发 | 2→2 | 0→0 | 0/0 |
| 合计 | **85→85** | **152,264→0** | **23/0** |

26 个候选结果的元数据由 8→2 列，包括 3 个空结果；23 个锁定结果仍为 18 列，正文 getter 合计仍为 152,264 字节。原 NoticeClaim 16 窗口 244→244 SQL、54 commit/1 rollback，原 FeishuDelivery 25 窗口 345→345 SQL、78 commit/0 rollback。精确比较只允许候选 SQL 模板、列元数据和候选正文 getter 指标变化；其余 SQL 顺序、连接/线程等价分组、事务结果和固定业务事实保持。比较器的 1 个合成正例及 14 个负向变异校验只证明比较协议，不是业务验收。

这不是整轮 Turn 或生产多用户容量测试。上述逻辑字节不代表 PG wire、TOAST/heap 页、实际内存或吞吐；现有索引也不能据此称为 index-only scan。

## 验证命令与来源

先在未改生产的 `dda1d7d` 上运行新夹具：2 项协议单元 + 22 项 PG 集成通过。之后只修改 Mapper 和两处单元 mock，成功 before/after 的 466 个输入中只有这两个文件变化；三份新夹具/观察器/校准源码及旧 SQL 计数器、NoticeClaim、FeishuDelivery 夹具保持相同摘要。原两套旧报告的默认 mode=after 标签保留，fresh before 身份由真实 runner、源码摘要、Git 生产 blob 和 JUnit 报告确认，不改写原始标签。

首次 before 中空结果校准单元因嵌套 Mockito stubbing 出错，2 项单元中 1 error，0 项集成执行。将 metadata mock 的创建移到 thenReturn 之前后重新冻结并通过；失败日志、XML、命令和输入摘要一并保留，不归咎于 Docker 或生产。

最终同次命令：

```bash
scripts/mvn.sh -B -ntp -o validate spotless:check package -Dit.test=NoticeCandidateQueryIT,NoticeClaimQueryIT,WorkerClaimQueryIT,TaskLifecycleIT,ExecutionHeartbeatIT,RecoveryIT,FeishuIT,WorkingContextIT,RecoveryReceiptQueryIT,FeishuDeliveryQueryIT,ExecutionPrivacyIT,PrivateAgentRuntimeIT,ExecutionBudgetQueryIT,TurnDatabaseBaselineIT failsafe:integration-test failsafe:verify
```

实际 XML：**1,320 单元 + 173 项、14 套选定集成 = 1,493 项**，失败/错误/跳过均为 0。相关范围涵盖通知锁等待与回滚、Worker 领取、生命周期、执行心跳、恢复、工作上下文、隐私、预算和整轮 SQL 基线。打包的 419 个生产 class、22 个资源与编译输出/源码资源字节一致，JAR SHA-256 为 `9e9ba45dda4ea76e64acec0f4a4266ced08b3d5273e0dd317e1de10b240dc61c`。证据保留真实进程退出、原始报告、前后 SQL、输入和产物摘要。

原提交前本地命令未执行完整项目 verify、46 项脚本测试套件、Compose、远程 CI、独立 CLI 或部署后 JAR 验收；后续远程结果单列，原本地证据保持。

## 后续热点

Outbox 批量投影仍逐 binding 读取并提交；队首竞争仍需多执行者锁等待测量。恢复扫描需大表 EXPLAIN/BUFFERS，连接池与执行心跳需测等待时间、续租延迟和租约余量。长工具链上下文写入与独立 Turn.result 正文读取也仍需单独测量，见 [Worker/Outbox 后续清单](worker-outbox-queries.md#后续五类数据库热点)。

## 远程交付补充

[run 40189599778](https://github.com/westbrook0904/FinanceClaw/actions/runs/40189599778) 针对 `e115834` 整条 completed/success：**1,320 单元 + 570 集成 = 1,890 Java、46 脚本和 44 部署检查通过**。原本地 1,493 项选定验收、失败记录与 SQL/结果指标保持；[远程证据](evidence/p3a14-remote-ci.json)独立保存，不覆盖后续 P3a.15。远程与本地产物未比较 JAR 字节身份。
