# 交互回答完成判断：P3a.16

**状态：本地验收通过，尚未推送。** 前置 `36459c4` 已推送，[run 40193745424](https://github.com/westbrook0904/FinanceClaw/actions/runs/40193745424) 整条 completed/success：1,894 Java、46 脚本、44 部署检查通过。[远程记录](evidence/p3a15-remote-ci.json)只对应 P3a.15，本阶段[本地证据](evidence/p3a16-implementation.json)独立保存。

## 当前实现

[AnswerInteractionService](../../src/main/java/com/financeclaw/app/interaction/AnswerInteractionService.java) 原先在写入回答后，读取整个 Turn 的完整交互历史，按当前 attempt 过滤并判断所有状态是否为 answered。随着暂停批次增加，每次回答会重复映射旧 response 正文。

现在 [InteractionGateway](../../src/main/java/com/financeclaw/app/interaction/port/InteractionGateway.java) 提供明确的 `existsUnansweredInteractions`，由 [MyBatisInteractionGateway](../../src/main/java/com/financeclaw/infrastructure/interaction/MyBatisInteractionGateway.java) 调用 [GovernanceMapper](../../src/main/java/com/financeclaw/infrastructure/governance/GovernanceMapper.java) 的 `existsUnansweredInteractionsByTurnAndAttempt`：

```sql
SELECT EXISTS (
  SELECT 1 FROM business.interactions
  WHERE turn_id = ? AND attempt_id = ? AND status <> 'answered'
)
```

只返回一个 boolean；服务取反后沿用原恢复入口。pending（含过期行）和 invalid 都阻止恢复，不加入 live、有效期或仅 pending 条件；Turn 与绑定 attempt 同时限定。空集合返回 false，对应原 filtered allMatch 的完成真值。回答 UPDATE 后仍用独立 SELECT，读取本事务刚保存的回答，没有合并为同一条 CTE。

command → Turn → interaction 锁序、owner/policy/grant/checkpoint/内容校验、提前幂等回放、恢复 CAS、事件和提交后唤醒保持。通用完整历史方法与 waiting 通知的 pending 专用读取保持，API、检查点及 AgentScope 恢复仍可读取所需回答。没有 fallback、缓存、索引、schema、队列或心跳变更。

## 验证范围与观察方式

先写入实施计划，再冻结新夹具，在 `36459c4` 原生产实现上运行 6 套、57 项真实 PostgreSQL 基线；随后只修改四个生产文件，并增强现有 HTTP 单元测试的明确 boolean stub、写答→探测→恢复顺序和回放无探测断言。两个新测试文件、既有四组查询夹具和 JDBC 计数器在前后保持同 SHA。

[InteractionCompletionQueryIT](../../src/test/java/com/financeclaw/p3/InteractionCompletionQueryIT.java) 的 6 项用例覆盖：原生 SDK 三批双审批的首末回答与最终 6 份报告/4 次 attempt；当前 waiting 批次中部分已答题的同 hash 回放/冲突；真实 command 锁下双回答；owner/revision/过期/取消门禁及过期 pending 同批问题；恢复写入故障回滚与重试；隔离的 8 项 mapper 谓词矩阵。invalid 来自真实取消，只调整过期场景 expires_at，不伪造正常 waiting 的 invalid+pending，不删除约束或扩大原预算。

并发测试通过 `pg_blocking_pids` 的阻塞图证明两个请求均等待真实 command 锁，要求两个不同回答非回放成功、仅一次 resuming 事件与 COMMAND 信号、resume_id 绑定最后一题，随后原生恢复完成。角色按实际 partial/final 语义配对；首答 oracle 从初始历史仅加入自身回答，不用最终全 answered 状态冒充首答时点。

[InteractionCompletionRecorder](../../src/test/java/com/financeclaw/p3/InteractionCompletionRecorder.java) 只观察两个精确 MyBatis statement 的四参数 Executor.query 返回集合，原样返回，不增加 SQL/getter。旧结果统计完整 Interaction 实体和 response UTF-8 逻辑长度；新结果要求恰一个非 null Boolean。观察序列与 JDBC execute 摘要逐项对齐，实际回答的探测必须紧接写答且同连接/线程；隔离矩阵单读事务单独计量。报告不含正文、ID、owner 或 bind 值。

## 前后实测

| 范围 | 窗口 | SQL 前→后 | Interaction 映射前→后 | Boolean 标量前→后 | response 逻辑字节前→后 | commit/rollback |
| --- | --- | --- | --- | --- | --- | --- |
| 实际回答判断（含并发、回滚与重试） | 7 | 102→102 | 32→0 | 0→7 | 131,892→0 | 6/1 |
| 隔离 mapper 边界矩阵 | 8 | 8→8 | 26→0 | 0→8 | 54,576→0 | 8/0 |
| 回放/冲突及门禁拒绝，无完成查询 | 6 | 27→27 | 0→0 | 0→0 | 0→0 | 1/4 |
| 合计 | **21** | **137→137** | **58→0** | **0→15** | **186,468→0** | **15/5** |

实体和标量是不同结果类型，不能把 58→15 写成相同类型的行数减少。统计只覆盖完成判断的结果，不表示锁定单个交互、SDK 状态或全链路不再读取正文。逻辑长度不等于 JDBC getter、PG wire、TOAST、内存分配、吞吐或扫描页。

旧 WaitingNotice、NoticeCandidate、NoticeClaim、FeishuDelivery 窗口分别保持 34/85/244/345 SQL。严格比较只允许 15 次精确历史查询模板替换为 EXISTS；其余有序模板、连接/线程分组、显式提交/回滚、oracle 和业务标志保持。沿用旧报告的历史 mode=after 标签，不改写成 fresh before；阶段身份另由 runner/Git blob/输入摘要确认。计数器未修改，原有四种未分类精确模板在新回答窗口逐项核对，含必需的 grant revision 读取；不笼统忽略 other。

## 命令与产物

最终同次命令：

```bash
scripts/mvn.sh -B -ntp -o validate spotless:check package -Dit.test=InteractionCompletionQueryIT,WaitingNoticeQueryIT,NoticeCandidateQueryIT,NoticeClaimQueryIT,WorkerClaimQueryIT,TaskLifecycleIT,ExecutionHeartbeatIT,RecoveryIT,FeishuIT,WorkingContextIT,RecoveryReceiptQueryIT,FeishuDeliveryQueryIT,ExecutionPrivacyIT,PrivateAgentRuntimeIT,ExecutionBudgetQueryIT,TurnDatabaseBaselineIT,GovernanceIT failsafe:integration-test failsafe:verify
```

实际 XML：**1,320 单元 + 17 套选定集成 208 项 = 1,528 项**，失败/错误/跳过均为零；before/after 两次 Maven 验收均首次成功。470 项输入无漂移，419 个生产 class、22 个资源与本次 JAR 字节一致。JAR SHA-256：`8023e559582f6df0e47a4bcebd8849535e484eb28bcf7d5ee0856f607855983b`。严格比较器的合成负例仅校验比较器，不计入业务用例。

本阶段未执行完整项目 verify、脚本测试套件、Compose、远程 CI、独立 CLI 或部署后 JAR 验收；前置远程全绿不替代本阶段验证，远程/本地 JAR 字节未比较。

## 剩余热点

原生恢复仍按需读取完整历史，其扫描与写入字节需要另测。Outbox 批量投影、通知队首竞争、恢复扫描计划及多用户连接池/心跳延迟继续见[后续清单](worker-outbox-queries.md#后续五类数据库热点)，不能由本阶段结果推导生产容量或公平性。
