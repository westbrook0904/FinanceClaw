# 通知领取双锁查询优化：P3a.13

**状态：已完成本地选定范围验收，尚未推送。** 前置 `24e74e5` 已推送，[run 40179982140](https://github.com/westbrook0904/FinanceClaw/actions/runs/40179982140) 整条成功：1,875 Java、46 脚本、44 部署检查通过。本阶段单独测量与验收。

## 实施范围

当前投影每个绑定分别锁绑定、按 owner/conversation 读 Turn、插入通知；waiting 再读取交互。这些不是同事务重复读取。将 20 个投影合成一个事务会扩大锁持有和回滚范围，因此保留逐项提交。本阶段只合并领取中的 binding、notice 两次锁定读取。

1. 在原 `24e74e5` 生产代码上建立真实 PostgreSQL before 基线。用独立连接和 `pg_blocking_pids`/NOWAIT 证明等待位置，观察 SQL 排除在业务计数之外。
2. 新 Mapper 通过两个依赖的 `MATERIALIZED` CTE，先按主键锁 binding，再按主键锁 notice；返回两个真实锁定行的类型化投影。不得把 pending、muted 或 revision 条件提前放进 SQL 过滤。
3. 应用层保留原检查顺序和抑制原因，随后 `tasks.get(owner, conversation, turn)` 仍是独立 SQL。CTE 不产生新快照，不能把非锁定 Turn 读取顺带放进同一语句。
4. 缺行时防御性跳过，不复用旧候选、不生成假的 suppressed 记录。候选仍算已处理工作，并受每轮 20 个候选上限约束。
5. 验证等待 binding 时尚未锁 notice、等待 notice 时已持 binding；等待期间的静音、绑定版本、notice 状态及 Turn 修订变更必须仍被发现。再覆盖双 owner 的 1/20 项投影、最多一次发送及第二项投影故障时第一项已提交保留。
6. 只允许两条锁查询替换为一条新模板；后续 owner/Turn 查询、pending→sending CAS、逐项投影事务、事务外发送和 unknown/submitted 不自动重发保持。
7. 原生产基线成功后再应用生产补丁，冻结源码，运行全量单元和相关集成，核对 SQL、输入与 JAR 字节，最后更新 README。

锁与快照设计参考 [PostgreSQL 16 隔离级别](https://www.postgresql.org/docs/16/transaction-iso.html)、[LockRows 执行器](https://github.com/postgres/postgres/blob/REL_16_STABLE/src/backend/executor/nodeLockRows.c)及 [CTE 扫描](https://github.com/postgres/postgres/blob/REL_16_STABLE/src/backend/executor/nodeCtescan.c)。源码推导不能替代本项目的真实数据库交错测试。

本阶段不改变 schema、投影事务、轮询频率、心跳、候选 payload、grant/隐私契约或发送资格撤回边界。当前通知领取验证业务 owner 与版本，没有新增执行 grant/隐私 epoch 校验；记忆遗忘也不等于删除业务 Turn/Journal。

## 验收记录

原 `24e74e5` 生产代码的 19 项真实 PostgreSQL 基线全部通过；修改后同次 **1,318 单元 + 13 套选定集成 170 项 = 1,488 Java** 全部通过，零失败、错误、跳过。

| 场景 | before SQL | after SQL |
| --- | ---: | ---: |
| 合法通知领取 | 5 | 4 |
| 等锁后 notice 已非 pending | 3 | 2 |
| 静音或绑定版本变化，抑制旧通知 | 4 | 3 |
| Turn 修订已变化，抑制旧通知 | 5 | 4 |
| 完成通知首次投影并发送 | 10 | 9 |
| waiting 首次投影并发送 | 11 | 10 |
| 首轮投影 20 项并发送一项 | 67 | 66 |
| 21 条旧通知与 1 条有效通知，两轮维护完整处理 | 94 | 72 |

新锁等待基线 9 个用例、16 个窗口 **274→244 SQL**，54 次显式 commit／1 次 rollback 保持。现有通知回归 10 个用例、25 个窗口 **395→345 SQL**，78 次显式 commit／0 次 rollback 保持。41 个窗口合计 **669→589**，严格比较只允许 80 处同连接、同线程的相邻 binding/notice 锁查询替换为精确 CTE；其余有序 SQL、连接/线程分组、事务结果及新报告的固定业务标志一致。这是多种隔离测试窗口的合计，不是单个用户 Turn 或生产容量数据。

等待 binding 时，独立连接仍能 NOWAIT 锁 notice；等待 notice 时，独立连接无法 NOWAIT 锁 binding。notice→unknown 在领取语句已开始且仍等待 binding 时独立提交，领取随后识别新状态；静音和绑定 revision 变化也触发原抑制分支。等待期间通过正常任务服务取消 Turn 后，后续独立查询发现新 revision 并抑制旧通知。完整 Notice、两个 revision、发送路由和正文由 JUnit 断言；第二项投影故障保留第一项提交，重试只补缺失项，每轮最多一次发送。新增一个单元用例验证缺行失败关闭，不冒称新增了数据库删除场景。

最终命令为：

```bash
scripts/mvn.sh -B -ntp -o validate spotless:check package \
  -Dit.test=NoticeClaimQueryIT,WorkerClaimQueryIT,TaskLifecycleIT,ExecutionHeartbeatIT,RecoveryIT,FeishuIT,WorkingContextIT,RecoveryReceiptQueryIT,FeishuDeliveryQueryIT,ExecutionPrivacyIT,PrivateAgentRuntimeIT,ExecutionBudgetQueryIT,TurnDatabaseBaselineIT \
  failsafe:integration-test failsafe:verify
```

before 使用专用 `-Dfinanceclaw.test.notice-locks-baseline=true test-compile`，选择 NoticeClaimQueryIT 和 FeishuDeliveryQueryIT。原生产 278 项输入与 Git blob 逐个一致；新 Notice fixture 与计数器前后字节相同。现有 Feishu fixture 适配 SQL 数量、模板断言和报告路径，明确不声称它前后同 SHA。该旧夹具在 fresh before 的原始 `mode=after` 是 P3a.10 的默认标签，原样保留；本阶段 before 身份由生产 SHA、runner、源码指纹和 19 项 JUnit 共同证明。

最终 463 项输入前后不变，418 项生产 class、22 项资源与本次 JAR 逐字匹配，包含 JAR 根目录的三份许可证。[本阶段证据](evidence/p3a13-implementation.json)保留两份原始 SQL 报告、严格比较、测试与产物身份；完整 Notice/正文没有写入 SQL JSON，新报告只导出固定布尔事实和计数，旧报告只包含 SQL 观测。

本阶段未执行其余集成、脚本用例、Compose、打包 CLI、独立部署 JAR 或新远程 CI；前置远程 1,875／46／44 独立保留。有限双 owner 积压不证明持续流量下的公平性，也不等于生产多用户认证验收。Outbox 批量投影、队首锁等待、扫描计划、多用户连接池和心跳续租延迟仍待专项测量。
