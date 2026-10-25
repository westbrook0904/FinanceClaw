# Worker 状态返回查询优化：P3a.12

**状态：已推送 `24e74e5`，远程完整 CI 通过。** 前置 `2de8537` 已推送，[run 40176176072](https://github.com/westbrook0904/FinanceClaw/actions/runs/40176176072) 整条 completed/success：1,866 Java、46 脚本及 44 部署检查通过。本阶段基线、源码和验收单独记录。

## 实施方案

Worker 原领取流程锁定 Command 和 Turn、更新 Command、插入 attempt、迁移 running 并写事件后，额外读取一次 Turn。目标仅移除成功领取末尾这次查询。

1. 在原 `2de8537` 生产代码上，先跑 10 项真实 PostgreSQL Worker 基线及 15 个 JDBC 窗口。使用本阶段专用开关，不混用 P3a.10 的历史 8 SQL 基线。
2. 普通 `change → transition → updateTurnState` 保持只写；新增 `changeAndReturn → transitionAndReturn → updateTurnStateAndReturn` 供领取需要真实更新行时使用，避免 completed 等路径额外回传正文。
3. Mapper 共享同一 DML 常量；返回分支追加完整 TURN_COLUMNS，并保留 affectData、刷新缓存与禁用查询缓存。普通分支不追加 RETURNING。
4. 两个服务入口共享前置事务/领域/交互失效检查与后置事件/Journal/记忆任务处理。返回空行立即 Fenced；全部副作用成功后才返回真实 Turn。
5. Lease 复用该返回值，移除只用于末尾读取的 QueryGateway 依赖及构造参数。保持 command → Turn 锁序、attempt、fence、事件与后续执行门禁。
6. 对比成功领取 7→6 SQL，空队列仍 1；attempt/Turn/event 写失败仍为 4/5/6，不能跟着成功总数错误下调。保留完整 Turn 回读等值、running 事件修订号、并发与 SKIP LOCKED 断言。
7. 源码冻结后运行全量单元与相关集成，核对输入及 JAR 字节，记录前后有序模板、连接/线程分组和事务结果，再更新 README。

本阶段不修改 schema、索引、Outbox 批量事务或心跳频率，不增加跨事务缓存或兼容构造器。

## 事务与返回值

两条迁移入口共享 `prepare` 和 `complete`，保持事务检查、领域检查、交互失效、状态更新、事件、最终会话记录、记忆任务的顺序。完成副作用继续使用原迁移输入；数据库返回行仅交还调用方，不能用它重新决定 Journal 或记忆内容。返回空行时在写事件前抛出 `Fenced`，交由原领取事务回滚。

`ExecutionLeaseService` 删除只用于最后回读的 `TaskQueryGateway` 依赖。领取仍按 Command 候选锁、Turn 锁、领取 Command、插入 attempt、更新 Turn、写事件执行。事件写入失败不能向外返回 Claim；事务提交后的通知、独立执行栅栏、隐私与权限检查保持。

源码入口：[领取服务](../../src/main/java/com/financeclaw/app/execution/ExecutionLeaseService.java)、[迁移服务](../../src/main/java/com/financeclaw/app/task/TaskTransitionService.java)、[Mapper](../../src/main/java/com/financeclaw/infrastructure/task/TaskMapper.java)。

## 验收记录

15 个公共 JDBC 窗口合计 **77→69 条 SQL**，成功领取 **7→6**，空队列仍 1。8 个成功窗口各删除末尾一次普通 Turn 回读；另外 10 处已执行的状态 DML 仅追加精确 RETURNING 模板，其中包含 Turn/事件写失败窗口。其余有序模板、初始 `FOR UPDATE`、连接/线程分组不变；15 个连接组、10 次 commit、5 次 rollback 保持。竞争测试按成功者/空结果角色配对，不强求相同线程赢得领取。

原 `2de8537` 生产输入上，10 个真实 PostgreSQL 用例全部通过。修改后同次 **1,317 单元 + 12 套选定集成 161 项 = 1,478 Java** 全部通过，零失败、错误、跳过；其中新增 8 个单元用例验证普通/返回入口分离、原副作用参数及顺序、空返回、事务和终态检查。单元使用真实 Spring 事务同步标志与测试事务管理器，只证明调用顺序和回滚信号，不代替 PostgreSQL 原子性。

Worker 10 项数据库用例验证完整 Turn 与窗口外数据库回读等值、running 事件的身份/修订号/状态/reason、并发唯一 attempt、SKIP LOCKED 和恢复元数据。attempt、Turn UPDATE、事件写入故障均整体回滚且可正常重试；失败窗口仍执行 4/5/6 条 SQL，不能按成功总数下调。

原生产 278 项输入与 `2de8537` Git blob 逐个一致，Worker 测试源码在 before/after 字节相同。最终 462 项 Java/资源/构建输入前后不变；417 项生产 class 和 22 项资源与本次 JAR 逐字匹配，包含位于 JAR 根目录的三份许可证。这只确认编译与打包身份，不代替独立部署 JAR 的完整链路验收。

本阶段先用专用 `-Dfinanceclaw.test.worker-turn-returning-baseline=true` 在原生产实现测量 7 条成功领取 SQL，再在修改后以默认模式验收 6 条。测试明确拒绝 P3a.10 旧 `query-baseline` 开关，旧 8 条基线及历史报告不重写。最终可运行命令为：

```bash
scripts/mvn.sh -B -ntp -o validate spotless:check package \
  -Dit.test=WorkerClaimQueryIT,TaskLifecycleIT,ExecutionHeartbeatIT,RecoveryIT,FeishuIT,WorkingContextIT,RecoveryReceiptQueryIT,FeishuDeliveryQueryIT,ExecutionPrivacyIT,PrivateAgentRuntimeIT,ExecutionBudgetQueryIT,TurnDatabaseBaselineIT \
  failsafe:integration-test failsafe:verify
```

首次修改后运行被沙箱限制，Mockito 无法动态附加 JVM；该次退出 1，未进入集成或打包阶段。原日志与报告完整保留，在允许 JVM 附加的环境中以相同输入、相同命令重跑成功，没有修改断言。失败归档的 runner 按 after 模式将 `jar_packaged_in_this_run` 写为 true，但该字段不代表实际完成打包；旧 JAR 不计入本阶段产物。

SQL 原始报告只包含固定阶段、调用成功标志、模板摘要和连接/线程/事务元数据，不包含完整 Turn 或事件正文；完整记录与事件一致性由两次 JUnit 断言独立验证，不能冒称是 SQL JSON 中的业务事实比较。窗口排除准备和验收回读，后台 Worker 关闭、记忆提取关闭、离线模型，不是整轮真实供应商或生产多用户容量验收。原始数据与构建身份见[本阶段证据](evidence/p3a12-implementation.json)。

原提交前最终命令覆盖全量单元与上述选定集成，没有执行其余集成、脚本用例、Compose 或本阶段远程 CI；后续远程结果单列如下。前置远程成功不替代本阶段验收。后续重点仍是 Outbox 批量投影与队首竞争、恢复扫描计划，以及多用户连接池等待和心跳续租延迟。

## 远程交付补充

[run 40179982140](https://github.com/westbrook0904/FinanceClaw/actions/runs/40179982140) 对 `24e74e5` 整条 completed/success：**1,317 单元 + 558 集成 = 1,875 Java、46 脚本和 44 部署检查通过**。原提交前本地 1,478 项及首次沙箱失败记录不改写；[远程证据](evidence/p3a12-remote-ci.json)独立保存，不覆盖后续 P3a.13。
