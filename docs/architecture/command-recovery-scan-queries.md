# P3a.19：命令过期候选的单次数据库截止点

**状态：已推送 `7b46102`，远程完整 CI 通过。** 前置 `5fe16cf` 已推送，[run 40248871652](https://github.com/westbrook0904/FinanceClaw/actions/runs/40248871652) 整条通过 1,911 Java、46 脚本、44 Compose 检查。[前置远程记录](evidence/p3a18-remote-ci.json)与本阶段[本地证据](evidence/p3a19-implementation.json)分别保存。

## 已核对的调用链

- TaskWorker.recover 默认每 2 秒独立调用 RecoverTaskService.recoverExpired，不受领取 idle backoff 或执行槽位是否占满影响；进程内 AtomicBoolean 只防同实例重入。
- CommandGateway.expired → MyBatisCommandGateway → TaskMapper.listExpiredCommandIds。变更前无锁发现 running 且 lease_until 到期的最早 50 个 ID。
- 每个候选使用独立短事务：锁 command，按当前 status/live 再检查；然后锁 Turn、读取绑定 attempt 的 sealed receipt。确定回执继续检查 checkpoint/policy 并收尾；不确定结果 blocked/unconfirmed_execution，禁止自动重放。
- 原 command → Turn 锁序、封存回执 id/commandId/fence 绑定、finishCommand 的 fence 递增、心跳双锁和实时 live 谓词保持。候选永远不是执行授权快照。
- RuntimeHealth 也调用相同候选读作为可读性探针；本轮不改其 API 或独立健康策略。

## 唯一生产改动

TaskMapper.listExpiredCommandIds 中：

```sql
-- 原
lease_until <= clock_timestamp()
-- 新
lease_until <= (SELECT clock_timestamp())
```

原 status='running'、ORDER BY lease_until、LIMIT 50、无锁、返回 ID 集合与 statement identity 保持；不新增索引/schema/API/cache，不修改 locked command 或 heartbeat SQL。

已有 `command_expiry ON business.commands(lease_until) WHERE status='running'`。新非关联标量子查询使当前候选查询最多捕获一次数据库时钟，由 InitPlan 参数驱动原索引范围；下文默认 planner 的真实计划验证了这一访问路径。

截止点是 InitPlan 实际求值时的数据库时间，不是精确客户端/statement-start，也不是 Java 时间。查询过程中才过期的命令可以留到后续扫描；默认 2 秒调度不是严格恢复 SLA。独立恢复事务继续实时重验原租约，绝不能把候选截止点带入授权、续租、fence 或提交判断。

普通 SELECT FOR UPDATE 的 live 投影等待时序与心跳专用双锁 SQL 是不同问题；本阶段没有修改或声称修复普通 fenced 的纯锁等待过期边界。

## 仪器与严格比较

JdbcExecutionCounter 和 ApplicationJdbcRecorder 的 command_expired_poll 分类在 before 前支持精确旧/新两条完整 SQL；两份 helper 从 before 到 after 必须同 SHA。不能泛化 substring 让未知 SQL 进入已知类别。

模板 SHA：

- old：`ca0387bb296498bc409bccd18444b1a9f5e566d5599d366f969584dfe76370ca`
- new：`7708dbca971c83e1dac1dfb3ecf045e77ad3eae0c65f7a36b54599994bc6807b`

仅测三个 raw cohort：新 CommandRecoveryScanQueryIT、WorkerClaimQueryIT、RecoveryReceiptQueryIT。旧 Worker 是当前 6 SQL 成功领取；旧 Receipt 是当前单次 receipt 读取，两组都按 current-after 语义验收，不重放旧优化变换。

只允许 family command_expired_poll 的精确 old→new 模板替换。其他有序 SQL、连接/线程等价分组、显式事务计数、固定事实与错误语义保持；Receipt 并发按 recovered=1/0 的 winner/loser 配对，Worker 并发按 claimed=true/false 配对。窗口外锁观察次数依原合同单列，不混入生产 SQL。

新 case 使用已明显过期和远期租约保证前后同工作量。若不注入实际 midscan 时序，文档只说明单 cutoff 的推导语义，不宣称已通过该时序实验。SQL 次数保持一条候选 SELECT；收益是候选扫描工作减少，不是 roundtrip 减少。

## 真实 PostgreSQL 测量设计

1. 正常双 owner 少量真实命令：queued/done/NULL/running live/expired 与到期排序匹配；背景 worker/recovery 关闭，调用生产候选/恢复入口，完整回执与任务事实在窗口外核验。
2. 候选发现后，另一事务持有 command 行锁并通过原 renew/finish 路径提交新版本；恢复等待后必须跳过新 live/done 命令，不能凭旧候选接管。既有心跳/Receipt/生命周期回归保持。
3. 缺少 sealed receipt 的已过期尝试只能 blocked，不模型重跑；已封存、检查点错误、fence/commandId 错配和回滚重试由 Receipt13 回归，不重复扩大矩阵。
4. 独立合成规模：20,000 future running，加少量真实 expired，使用独立 Conversation/Turn 保持 FK/唯一约束。这是明确合成的查询规模负载，不证明真实 Worker 调度或容量；32 是 TaskWorker 配置上限，不是用户受理或 LeaseService.claim 上限。另以 3 条真实与 48 条合成组成的 51 个到期键验证最早 50 边界，不能在 sparse3 中自证 LIMIT50。
5. 在默认 planner、正常 ANALYZE 下对精确 old/new SQL执行 EXPLAIN (ANALYZE,BUFFERS,VERBOSE,FORMAT JSON)。报告只导出安全节点统计、索引名及派生布尔，不输出 Output/Filter/Index Cond 原文、ID、owner、bind 或截止点时间。

验证要求：新 SQL 的 InitPlan Result 在规模样本实际 loops=1/rows=1，输出包含数据库时钟；command_expiry 实际访问具有 lease_until 的参数范围 IndexCond。旧方案可能已利用 ORDER BY 索引但仍逐 live 候选过滤，不能把“旧查询没用索引”当作预设。小空表可能不执行 InitPlan，不对所有窗口硬要求 loops1。

分别记录每节点 rows/loops/removed/buffers；根 buffer 已包含子树，不求所有节点和。不以单次时间或 buffer 变化宣称物理 IO、吞吐、零 heap 或通用容量。已有 50 个 expired 时旧查询也能及早停止，收益可能很小；无 expired 或不足 50 且有大量 live 的场景更值得测。

## 实测结果

| 观测 | 原逐行时钟 | 新单次 cutoff |
| --- | --- | --- |
| 默认访问路径 | command_expiry Index Scan，逐行 Filter | 同一索引，InitPlan 参数范围 |
| 返回行 / loops / 过滤行 | 3 / 1 / 20,000 | 3 / 1 / 0 |
| 根 shared hit / read blocks（包含子树） | 420 / 0 | 3 / 0 |
| 扫描节点 shared hit / read blocks | 420 / 0 | 3 / 0 |
| 新业务窗口 SQL / 显式 commit / rollback | 19 / 3 / 0 | 19 / 3 / 0 |
| Worker 窗口 SQL / commit / rollback | 69 / 10 / 5 | 69 / 10 / 5 |
| Receipt 窗口 SQL / commit / rollback | 159 / 11 / 4 | 159 / 11 / 4 |

规模为 20,000 条明确合成的 future running，以及 3 条生产受理/领取的已过期命令；合成行有独立 Conversation/Turn，保持 FK 与唯一约束，没有 attempt，不进入恢复或 SDK。前后原生产 mapper 返回同一逻辑排序结果，完整状态快照保持。两边都用了原索引；收益是排除未来租约时不再遍历它们。新计划实际 Result InitPlan 为 1 row/1 loop，参数形成 lease_until Index Cond；原条件原文、clock 值、正文和身份不导出。逻辑 buffer 观测不代表物理 IO 或生产容量。

独立批次实验把 48 条合成行设为明确到期，加上 3 条真实过期行，51 项中严格返回最早 50 项；这是只读 mapper 边界，不证明恢复器完整排空或跨实例公平性。双 owner 的 7 个真实命令覆盖 queued/done/未来/NULL/到期顺序；续租和完成先在有效 lease 内生产写入，保持未提交直至旧 lease 在数据库时钟下到期。恢复真实等待行锁，提交后新 live/done 版本完整保留，不读取 Turn 或 receipt。已执行未封存任务只转 blocked/unconfirmed_execution，原 SDK、预算、attempt 和用户记录保持，不重跑模型或新增 assistant。

长事务测试在 transaction_timestamp 早于 lease 时等待真实数据库时间到期，再只执行一次生产 mapper，证明该查询不是事务起点的 now。它没有注入实际扫描中恰好到期的时序；“cutoff 后到期可延后”是 SQL 语义边界，默认 2 秒 fixedDelay 不是发现/完成 SLA。

三个 cohort 共 46 窗口、247 SQL，前后次数和事务保持；只允许新组 10 次、Receipt 19 次精确候选摘要替换，其他有序模板、连接/线程分组、业务事实和异常保持。Worker 并发按 claimed 角色、Receipt 并发按 recovered 角色配对；窗口外探锁次数单独记录。比较器 3 个正例与 47 个负变异通过。新测试及两个记录器前后同 SHA；474 输入的唯一前后差异为 TaskMapper，源码中其他生产逻辑与所有 schema 保持。419 class、22 资源和 3 份根许可证与本次 JAR 字节一致。

## 命令与验收边界

实际 before 为 3 套 28 项集成；after 为 83 套 1,320 项单元加 22 套 255 项选定集成，共 1,575 项，零失败/错误/跳过。两次 Maven 均首次成功，无编排失败。before 专用开关只适用于 5fe16cf 原生产查询增加本次同一夹具后的历史基线，不能用于当前新 SQL：

```bash
scripts/mvn.sh -B -ntp -o validate spotless:check \
  -Dfinanceclaw.test.command-recovery-scan-baseline=true test-compile \
  -Dit.test=CommandRecoveryScanQueryIT,WorkerClaimQueryIT,RecoveryReceiptQueryIT \
  failsafe:integration-test failsafe:verify

scripts/mvn.sh -B -ntp -o validate spotless:check package \
  -Dit.test=CommandRecoveryScanQueryIT,NoticeRecoveryQueryIT,ProjectionDiscoveryQueryIT,InteractionCompletionQueryIT,WaitingNoticeQueryIT,NoticeCandidateQueryIT,NoticeClaimQueryIT,WorkerClaimQueryIT,TaskLifecycleIT,ExecutionHeartbeatIT,RecoveryIT,FeishuIT,WorkingContextIT,RecoveryReceiptQueryIT,FeishuDeliveryQueryIT,ExecutionPrivacyIT,PrivateAgentRuntimeIT,ExecutionBudgetQueryIT,TurnDatabaseBaselineIT,GovernanceIT,AdmissionIT,BacklogIT \
  failsafe:integration-test failsafe:verify
```

原始归档 `/tmp/financeclaw-p3a19-before`、`/tmp/financeclaw-p3a19-after`；仓库证据保存来源指纹、XML/日志统计、raw 安全投影与比较结果，原始 JAR/报告不纳入 Git。模型为 offline；本轮未跑完整项目 verify、脚本套件、Compose、新远程 CI、独立部署 JAR、真实 provider 或飞书 HTTP，前置远程成功只对应 5fe16cf。

## 后续

密集过期命令的 50 项批次推进、多实例重复候选与锁等待、sending 集合规模和索引膨胀、Hikari 等待、心跳续租延迟及租约余量仍需独立验证。原普通锁读取的时钟投影等待边界也未在本轮改变；不得把候选 cutoff 复用为授权/提交/续租依据。


## 远程交付补充

[run 40252918781](https://github.com/westbrook0904/FinanceClaw/actions/runs/40252918781) 对应 `7b46102` 的 verify 和 delivery 均成功：1,916 Java、46 脚本、44 Compose 检查通过。[远程记录](evidence/p3a19-remote-ci.json)与上方原提交前本地范围独立保存。后续[并发资源改造](concurrent-recovery-resources.md)改变恢复领取方式，原只读扫描的历史测量不代表新恢复事务的 SQL 成本。
