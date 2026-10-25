# 通知投影候选与空等待项：P3a.17

**状态：已推送 `cc90a6d`，远程完整 CI 通过。** [run 40243547522](https://github.com/westbrook0904/FinanceClaw/actions/runs/40243547522) 整条 completed/success：1,320 单元 + 586 集成 = 1,906 Java、46 脚本、44 Compose 检查通过。[P3a.17 远程记录](evidence/p3a17-remote-ci.json)只对应该已交付提交；原提交前[本地证据](evidence/p3a17-implementation.json)独立保存。前置 `9d33018` 的 [run 40207741693](https://github.com/westbrook0904/FinanceClaw/actions/runs/40207741693) 及其 1,900/46/44 记录仍保留为历史基线，不用来替代后来源码的验收。

## 问题与实现

通知发现本来已经排除同 Turn/revision 的非 suppressed 通知。实际遗漏是 waiting 问题全部过期时，发现仍返回 binding；锁内依次读取 binding、owner Turn、pending 后只能渲染空文案，不会生成通知。最老 20 个空项会反复占满 LIMIT 20，让后面另一用户的有效通知不能前进，并使空闲退避失效。

[FeishuMapper](../../src/main/java/com/financeclaw/infrastructure/channel/FeishuMapper.java) 仅在发现阶段增加：

```sql
AND (t.status <> 'waiting' OR EXISTS (
  SELECT 1 FROM business.interactions i
  WHERE i.turn_id = t.id AND i.status = 'pending'
    AND i.expires_at > clock_timestamp()
))
```

不增加 attempt/release/grant 条件，保持与通知 renderer 的整个 Turn live pending 集合一致。原 mute、状态、非 suppressed 去重、排序和 LIMIT 20 保持。发现只返回定位 ID，不提供权限或发送资格。

[OperationsMapper.UNPROJECTED](../../src/main/java/com/financeclaw/infrastructure/task/OperationsMapper.java) 同步资格，让 notification_bindings 与 unprojected_notifications 对应实际通知工作。普通 Turn backlog、pending_interactions 和 waiting 状态保持：过期未答仍是未完成交互，不能因通知队列为空就报告 owner drained。

在 [V1 新库初始化定义](../../src/main/resources/db/migration/V1__initialize_application.sql) 增加部分索引 `interaction_pending_turn_expiry ON business.interactions(turn_id,expires_at) WHERE status='pending'`。41 表不变，没有新增升级兼容入口。索引缩小当前 pending 的 Turn 查找范围；实时到期过滤仍使用数据库时钟，answered/invalid 变更与新 pending 写入会维护索引。

[FeishuDelivery](../../src/main/java/com/financeclaw/channel/FeishuDelivery.java) 源码字节保持：每 binding 独立事务，先锁绑定，再用独立 SQL 读取 owner Turn、pending/live 并重新渲染。发现后到期、静音、重绑、取消或 revision 改变由锁后状态决定。领取、unknown/submitted_unverified、不确定投递不自动重发及事务外 SDK 发送保持。

## 真实 PostgreSQL 验证

先写实施计划，在 `9d33018` 原生产源码上运行 8 套、69 项 PG 基线，再应用三份生产文件。两份新增测试与所有既有查询夹具/计数器前后同 SHA。before/after 均首次成功，最终 **1,320 单元 + 20 套选定集成 245 项 = 1,565 项**，失败/错误/跳过均为零。

[ProjectionDiscoveryQueryIT](../../src/test/java/com/financeclaw/p3/ProjectionDiscoveryQueryIT.java) 的 6 项用例通过正常受理、原生 SDK 等待/审批/恢复和生产 scheduled/poll 路径，覆盖单项及 20 项全过期、同协调器提交唤醒、20 空前缀后的另一 owner、21 个有效项的批次边界与有限排空、部分已回答、扫描后到期/静音/重绑/取消修订。场景模型使用固定脚本驱动原生 SDK；末端 send 使用替身，不代表远端模型或飞书 HTTP 验收。过期时间和 created_at 顺序由隔离测试控制；旧 NoticeClaimQueryIT 继续验证第二项故障保留第一项提交与重试，没有合并投影事务。

[ProjectionDiscoveryRecorder](../../src/test/java/com/financeclaw/p3/ProjectionDiscoveryRecorder.java) 观察精确 MyBatis discovery 返回集合，记录数量并与 JDBC execute 逐项对齐；一次性屏障在扫描返回后、投影加锁前让另一线程提交真实变更。该屏障不是证明数据库行锁竞争，行锁/快照行为仍由原 PG 锁用例验收。报告不含 binding/owner/正文或 bind 值。

### 21 个业务窗口

| 窗口 | SQL 前→后 | commit 前→后 |
| --- | --- | --- |
| single-expired-first | 5→2 | 1→0 |
| single-expired-repeat | 5→0 | 1→0 |
| single-expired-maintenance | 6→3 | 1→0 |
| twenty-expired-first | 62→2 | 20→0 |
| twenty-expired-repeat | 62→0 | 20→0 |
| twenty-expired-maintenance | 63→3 | 20→0 |
| new-waiting-wakeup | 67→10 | 21→2 |
| expired-prefix-first | 62→10 | 20→2 |
| expired-prefix-second | 62→2 | 20→0 |
| expired-prefix-third | 62→0 | 20→0 |
| live-first-twenty | 86→86 | 21→21 |
| live-remaining-one | 10→10 | 2→2 |
| live-finite-drain | 114→114 | 19→19 |
| live-empty-repeat | 2→2 | 0→0 |
| partial-live-projection | 10→10 | 2→2 |
| post-discovery-expiry | 5→5 | 1→1 |
| post-discovery-mute | 3→3 | 1→1 |
| mute-reawaken | 10→10 | 2→2 |
| post-discovery-rebind | 3→3 | 1→1 |
| rebind-new-waiting | 10→10 | 2→2 |
| post-discovery-turn-revision | 9→9 | 2→2 |
| 合计 | **718→294** | **197→57** |

全部窗口 rollback 为零。20 个全过期项的首轮由 62→2 SQL，紧邻重复由 62→0，显式 maintenance 由 63→3；expired pending 与 waiting 保持。20 个空前缀后的另一 owner 三 tick 合计 **186→12 SQL、0→1 次发送**：前后业务推进刻意不同，不能把该结果称为相同工作量的吞吐提升，也不证明全局跨 owner 公平性。21 个有效项仍先投影最多 20 个、每 tick 最多外部发送一次并逐项提交，路由保持隔离。

运维两次观察在真实暂停受理后进行：过期 pending=1/20、waiting=1/20、Turn backlog=1/20 保持，drained 都为 false；只将 unprojected_notifications/notification_bindings 由 1/20 缩为 0。诊断和 oracle 在测量窗口外，不混入上述 SQL 次数。

旧五组 completion、waiting、candidate、notice、delivery 分别保持 **137/34/85/244/345 SQL**。严格比较只允许其中 discovery 的精确模板替换，其他有序 SQL、连接/线程分组、事务、结果和固定业务事实保持。原报告 mode=after 历史标签不改写；fresh before 身份由 runner、源码与 Git blob 确认。

## 索引依据与规模实验边界

独立计划实验在 21 个真实 waiting binding 之外，使用一个真实已完成批次的外键增加 20,000 条明确标记的合成 answered 历史。这些副本不代表真实 SDK 调用，也不交给恢复链路。默认 planner、ANALYZE 和精确 SQL 下：

- 原生产发现返回最老 20 个空项，根节点 hit+read=3；它完成的是旧资格查询。
- 新 SQL 的无索引预览正确返回有效尾项，但 interactions Seq Scan 执行 21 loops，Rows Removed by Filter 平均每 loop 19,084，根节点 shared hit+read=36,469。
- 新生产 SQL 与部分索引仍返回该有效项，实际使用索引的 turn_id 条件，根节点 shared hit+read=58；没有 interactions Seq Scan。

新 SQL 无索引预览是测试实验，未冒称旧生产执行过该 SQL。采用索引的决定绑定已封存 before 计划，再由 after 实际访问路径确认。只导出安全节点统计与派生布尔，不归档可能含标识的 Filter/Index Cond 原文。根节点 buffer 数包含子树，未叠加所有节点；Rows Removed 为每 loop 平均且经 PG 取整，不当作精确总扫描行数。

这些是隔离数据下的逻辑共享块观察，不能当作生产物理 IO、延迟或容量结论。`clock_timestamp()` 仍作为实时过滤，不宣称 expires_at 成为时间范围 Index Cond，也不宣称零 heap 访问。

## 命令与产物

```bash
scripts/mvn.sh -B -ntp -o validate spotless:check package -Dit.test=ProjectionDiscoveryQueryIT,InteractionCompletionQueryIT,WaitingNoticeQueryIT,NoticeCandidateQueryIT,NoticeClaimQueryIT,WorkerClaimQueryIT,TaskLifecycleIT,ExecutionHeartbeatIT,RecoveryIT,FeishuIT,WorkingContextIT,RecoveryReceiptQueryIT,FeishuDeliveryQueryIT,ExecutionPrivacyIT,PrivateAgentRuntimeIT,ExecutionBudgetQueryIT,TurnDatabaseBaselineIT,GovernanceIT,AdmissionIT,BacklogIT failsafe:integration-test failsafe:verify
```

472 项输入无漂移，419 个生产 class、22 个资源（含三份根许可证）与本次 JAR 字节一致；SHA-256 为 `9663e3f7283e17c82ab5e8e1e04c3b6576d1955cfde3bba04ac1308016c2a630`。比较器合成正负例只校验仪器，不计入业务测试数。

原提交前本地没有运行完整项目 verify、脚本测试套件、Compose、新远程 CI、独立 CLI 或部署后 JAR 验收；前置 9d33018 的远程全绿不替代这些未推送源码的验收，远程与本地 JAR 字节未比较。

## 后续

有效候选仍逐 binding 投影，集合读取、通知队首竞争、恢复扫描规模、多用户连接池及心跳续租延迟仍待独立测量。suppressed 重激活和未知投递规则保持原实现，本阶段没有重做通知生命周期；[后续五类热点](worker-outbox-queries.md#后续五类数据库热点)继续保留。

## 远程交付补充

原提交前本地命令只覆盖选定范围；随后 `cc90a6d` 的完整远程 CI 已覆盖 1,906 Java、46 脚本和 44 Compose 检查。远程成功不扩大原本地命令范围，也不验证后续 P3a.18；远程与本地 JAR 字节未比较。
