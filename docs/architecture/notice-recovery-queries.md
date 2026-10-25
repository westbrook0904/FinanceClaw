# P3a.18：失联 sending 通知恢复的部分索引

**状态：本地验收通过，尚未推送。** 前置 `cc90a6d` 已推送，[完整远程 CI](https://github.com/westbrook0904/FinanceClaw/actions/runs/40243547522) 通过 1,906 Java、46 脚本及 44 Compose 检查。[前置远程证据](evidence/p3a17-remote-ci.json)与本阶段[本地证据](evidence/p3a18-implementation.json)分别保存。

## 实现范围

唯一生产变更为 V1 新库初始化定义新增：

```sql
CREATE INDEX channel_sending_notice
ON channel.notices(started_at) WHERE status='sending';
```

不增加新表、迁移兼容入口、Java/API、缓存、轮询或分片；FeishuMapper 和 FeishuDelivery 字节保持。原 UPDATE 模板 SHA-256 为 `3b9fffdaa266baac83b0e5be30e176e9ef16d49d65dbf2527856964cd346e2ce`。

现有调用为独立 scheduledRecovery（默认 5 秒）及 public poll 的 recover。通知空闲退避不能停掉恢复。同进程 AtomicBoolean 仅防恢复重入；跨进程仍由 PostgreSQL UPDATE/行锁及原谓词决定。pending→sending 时原 SQL 设置 started_at；失联 sending 超过原 60 秒后只改 unknown/worker_lost，绝不自动回 pending。

## 索引语义和限制

- 变更前的通知部分索引仅覆盖 pending(created_at)，主键和 Turn/revision 唯一约束不覆盖 sending 过滤。命令 expiry 已有 running 部分索引，不在本轮重复修改。
- 新索引把恢复扫描范围限制在当前 sending 集合。`clock_timestamp()` 保持实时 VOLATILE 过滤，不能承诺 started_at cutoff 成为范围 Index Cond。
- started_at 为 NULL 的 sending 仍不满足原 SQL；保留负例事实，不把它描述为被成功恢复，也不新增修复语义。
- 每次恢复仍是一条 UPDATE，不能宣称 SQL 次数下降。UPDATE 状态变化和 started_at 写入需要维护索引；大量 sending 或行锁争用仍可能昂贵。
- unknown/submitted_unverified 不自动重发。恢复不授予发送或授权资格，不新增 binding 锁，不放宽原外部调用边界。

## 新真实 PG 用例边界

新增 NoticeRecoveryQueryIT 五项用例，使用生产 projection/claim/恢复及最终 send 替身；改变 started_at 和故障触发器仅作为隔离测试控制。

1. 双 owner 状态矩阵：过期 sending 成为 unknown；未过期/future/NULL sending 及其他状态保持；重复恢复幂等、不发送。
2. 通知 idle 不阻断独立恢复；send 替身在 gate 暂停期间，通过测试控制的失联时间恢复为 unknown；迟到成功/失败不能覆盖 unknown，新协调器不能重发。
3. 真实 PG 行锁等待：另一事务把 sending 改为终态或延长 started_at 后，恢复仍按 PostgreSQL 原 UPDATE 谓词处理当前版本。
4. 多行 UPDATE 触发故障全部回滚，重试才一次性成功；不引入局部提交或修复队列。
5. 独立规模实验：20,000 条明确标记的合成非 sending 历史（实际 submitted_unverified，不等于已送达）加少量真实 sending。默认 planner、同原 UPDATE、同匹配集合；EXPLAIN ANALYZE 会实际更新，必须在事务内核结果后 rollback，核对行全部恢复。合成条目不冒称真实投递。

EXPLAIN UPDATE 没有 RETURNING，ModifyTable 根节点 Actual Rows=0 不能当作零更新。匹配/更新数由安全状态计数与事务内 readback 验证。安全计划节点分别记录行、循环、过滤、buffer、索引名与派生条件布尔，不输出 SQL 条件、正文、ID/owner/bind。根 buffer 包含子树，禁止累加所有节点；不以一次耗时证明物理 IO、吞吐或容量。

## 比较协议

- 新 recovery 报告 `notice-recovery-sql/1`：所有测量窗口前后原 SQL 模板、有序 SQL、连接/线程分组、显式 commit/rollback、完成/故障和固定业务事实必须相等。没有语句替换白名单。
- common 六组为 P17 discovery、P16 completion、P15 waiting、P14 candidate、P13 notice、delivery，均当前 mode=after/after。业务窗口和运维事实完全保持，不再执行旧阶段的模板转换。
- P17 discovery 的独立 scale 各自执行现有 after validator：同 new SQL/有效结果、pending 部分索引访问及实时 clock 过滤保持；fresh PG 计划的安全节点统计允许并列不同，明确列为独立实验例外，不能泛化忽略业务字段。
- 既有 NoticeClaim 窗口外锁探测次数和 Delivery elapsed_nanos 只按原仪器合同记录，不据此作性能结论；生产 SQL 和结果不放宽。
- 先以封存 before 验证结构并做合成正负变异自测；正式 after 由 root 宣布 sealed 后才能打开。前后输入身份、原 runtime 字节、JAR/class/resource 及真实 Maven 成功另由 evidence writer 校验。

实际 before 为 7 套 43 IT，after 为 83 套单元 1,320 项及 21 套选定集成 250 项，均由本次 XML 统计，零失败/错误/跳过。模型为 offline，末端发送为替身；本轮未运行完整项目 verify、脚本套件、Compose、新远程 CI、独立部署 JAR、真实 provider 或飞书 HTTP。

## 实测结果

| 观测 | 未加索引 | 加索引 |
| --- | --- | --- |
| 新业务窗口 SQL / 显式 commit / rollback | 32 / 2 / 0 | 32 / 2 / 0 |
| 规模 UPDATE 默认扫描 | Seq Scan notices | Index Scan channel_sending_notice |
| 扫描节点返回行 / loops / 过滤行 | 1 / 1 / 20,003 | 1 / 1 / 2 |
| 扫描节点 shared hit / read blocks | 667 / 0 | 5 / 0 |
| 根 shared hit / read blocks（包含子树） | 679 / 0 | 17 / 0 |
| 实际匹配更新 / ModifyTable Actual Rows | 1 / 0 | 1 / 0 |
| EXPLAIN 后显式 rollback、完整状态恢复 | 通过 | 通过 |

规模负载还包含 1 条真实非 sending 历史，总共 20,004 条通知。三条真实 sending 分别为超时、未来一天、NULL started_at。前后匹配集合与完整 Notice oracle 保持，不把合成历史称为真实投递。volatile cutoff 保留 Filter，NULL 仍跳过，索引并不把到期时间变成范围 Index Cond。这里测量的是逻辑 buffer 观测，不是物理磁盘读、吞吐或容量。

16 个新窗口的有序 SQL 模板、连接/线程分组、显式事务和固定业务事实完全一致；旧 discovery/completion/waiting/candidate/notice/delivery 六组 SQL 分别保持 294/137/34/85/244/345。旧独立 P17 计划的逐节点统计单独记录，其 live-query 约束保持；测试探锁次数和 elapsed_nanos 不作为性能结果。比较器 3 个正例、50 个负变异自测通过。生产 Java 字节未变，473 输入在各自执行前后不变，前后唯一输入变化为 V1 索引定义；419 class、22 资源和根目录 3 份许可证逐字节匹配本次打包产物。

失败触发器在整条 UPDATE 处理完三行后抛错，完整快照证明修改全部回滚；这里 JDBC 显式 rollback 为 0，不等于未回滚。晚结果测试的新协调器是同 JVM 新对象，不能扩为进程重启验收。未过期负例使用未来一天，不能扩为严格秒级 TTL 临界测试。

## 本地命令与记录

before 在 cc90a6d 的生产源码、增加同一新测试后执行；该专用开关要求新索引尚未存在，只适用于此历史基线。after 在本阶段索引源码上执行：

```bash
scripts/mvn.sh -B -ntp -o validate spotless:check \
  -Dfinanceclaw.test.notice-recovery-baseline=true test-compile \
  -Dit.test=NoticeRecoveryQueryIT,ProjectionDiscoveryQueryIT,InteractionCompletionQueryIT,WaitingNoticeQueryIT,NoticeCandidateQueryIT,NoticeClaimQueryIT,FeishuDeliveryQueryIT \
  failsafe:integration-test failsafe:verify

scripts/mvn.sh -B -ntp -o validate spotless:check package \
  -Dit.test=NoticeRecoveryQueryIT,ProjectionDiscoveryQueryIT,InteractionCompletionQueryIT,WaitingNoticeQueryIT,NoticeCandidateQueryIT,NoticeClaimQueryIT,WorkerClaimQueryIT,TaskLifecycleIT,ExecutionHeartbeatIT,RecoveryIT,FeishuIT,WorkingContextIT,RecoveryReceiptQueryIT,FeishuDeliveryQueryIT,ExecutionPrivacyIT,PrivateAgentRuntimeIT,ExecutionBudgetQueryIT,TurnDatabaseBaselineIT,GovernanceIT,AdmissionIT,BacklogIT \
  failsafe:integration-test failsafe:verify
```

原始归档为 `/tmp/financeclaw-p3a18-before`、`/tmp/financeclaw-p3a18-after`；仓库证据保存输入、XML、日志、JAR、raw 报告、比较器和依赖的身份摘要及验收统计，原始制品不纳入 Git。两次 Maven 均首次通过。一次本机 Perl locale 错误使补丁命令未执行，随后的 after runner 在创建归档和构建前拒绝未变更的生产范围；改用 Python 核对并应用补丁后正常启动，不计作失败 Maven。

## 剩余边界

大量 sending、长事务/多实例锁等待、索引膨胀与状态写入维护、命令过期恢复大表计划及有界批次仍待测量。当前命令恢复已有 running(lease_until) 部分索引，本轮不重复添加。连接池借用等待、心跳续租延迟和租约余量继续作为独立热点；不通过降频、缓存 live 或未知投递自动重发减少成本。
