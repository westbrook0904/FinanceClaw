# 恢复回执查询优化：P3a.11

**状态：2026-10-25 已本地验收，未推送。** 前置 `0c789b8` 与 `06ec674` 已推送，GitHub [run 40173421896](https://github.com/westbrook0904/FinanceClaw/actions/runs/40173421896) 整条 completed/success：1,843 Java、46 脚本及 44 部署检查通过。本阶段另行通过 **1,309 单元 + 150 选定集成 = 1,459 项**，不沿用前置远程结果宣称新实现已通过 CI。

## 实现与权威边界

`RecoverTaskService` 原来在 command → Turn 持锁事务内读取 attempt 的回执，发现已封存后，再调用 `ExecutionCompletionService.finishReceipt` 重复读取相同 attempt。本阶段仅改这两个 Service：内部入口改为三参 `finishReceipt(Command, Turn, Receipt)`，恢复直接传同事务已经读取的对象；正常 `finish` 仍在原锁定位置读取一次。没有兼容重载、跨事务缓存、Mapper/SQL 或 schema 变更。

调用方始终先锁 Command，再锁 Turn。合法封存也要取得同一 Command 锁，且 SQL 只允许 `outcome IS NULL` 时写入；本事务复用已读 sealed receipt 不允许跨事务保留或据此推测 SDK 完成。未封存、缺回执仍转为 `blocked/unconfirmed_execution`，不重放未知执行。

三参入口先核对回执 id、commandId、fence 和非空 outcome，再按原顺序检查 SDK 检查点与实时 policy。id 原由按 attempt 主键读取隐含保证，传参后显式保留；**commandId 一致性是新增防护**，旧代码与独立外键没有保证这种配对，不能将它描述为原有行为。

检查点不匹配、隐私/授权变化、取消状态的错误优先级保持；policy 的 owner/source/grant 等实时检查不复用。恢复面对过期 running Command，不能给公共收尾逻辑新增 `live=true` 要求。状态、事件、Journal 和 Command 完成保持同事务；触发器故障或身份不匹配会回滚。心跳和恢复候选扫描未改动。

## 实际 JDBC 对比

准备、执行/封存、故障注入与最终核查都在测量窗口之外；窗口只包真实 `recoverExpired()` 或正常 `finish()`。每条 JDBC execute（含失败）按模板摘要、类别、连接与线程记录。

| 同一业务窗口 | before SQL | after SQL | 回执读取 |
| --- | ---: | ---: | --- |
| 已封存 completed 恢复 | 16 | 15 | 2→1 |
| 已封存 waiting 恢复 | 14 | 13 | 2→1 |
| 检查点墓碑/版本失配 | 15 | 14 | 2→1 |
| 回执 fence 失配并回滚 | 5 | 4 | 2→1 |
| Journal 插入失败并回滚 | 15 | 14 | 2→1 |
| 移除触发器后同回执重试 | 16 | 15 | 2→1 |
| 两个恢复者竞争同一命令，合计 | 18 | 17 | 赢家 2→1，输家 0 |
| 无执行/SDK 未封存/缺回执 | 8 | 8 | 1→1 |
| 正常有租约的 finish | 15 | 15 | 1→1 |
| 恢复后再次空扫描 | 1 | 1 | 0 |

19 个公共测量窗口合计 **159→151 条 SQL，20→12 次回执读取**。恰好 8 个已封存恢复窗口各删除第二次同模板、同事务回执读取，其余有序 SQL、连接/线程分组、提交/回滚、异常类型、恢复数量与安全业务事实一致。并发按成功/未处理角色配对，允许线程赢家互换。

合计包括正常、失败注入、重试及空扫多个窗口，不代表一轮用户请求的 SQL 或生产吞吐提升。新增两个 after-only 错绑负例另计，各 4 条 SQL、1 次回执读取和 1 次回滚；它们不计入查询节省，也不声称旧实现已经拒绝。

## 验证范围

原 `06ec674` 生产输入 278 项逐个与 Git blob 核对一致，先运行 **11 项真实 PG 基线**，exit 0、零失败/错误/跳过。之后只在新 PG 类追加两项 commandId 错配防护；before 与 after 测试文件并非字节完全相同。

最终同次命令 exit 0，**1,309 单元 + 150 选定集成 = 1,459 项**，零失败、错误、跳过。461 项构建输入在执行前后不变，417 个生产 class 和 22 个资源与本次 JAR 字节匹配，3 份许可证按 JAR 根 `META-INF/licenses` 核对。原始报告、输入摘要、JAR 指纹及严格比较见[本阶段证据](evidence/p3a11-implementation.json)。

| 集成套件 | 用例 |
| --- | ---: |
| RecoveryReceiptQueryIT | 13 |
| TaskLifecycleIT | 25 |
| ExecutionHeartbeatIT | 14 |
| RecoveryIT | 13 |
| FeishuIT | 12 |
| WorkingContextIT | 20 |
| WorkerClaimQueryIT | 9 |
| FeishuDeliveryQueryIT | 10 |
| ExecutionPrivacyIT | 14 |
| PrivateAgentRuntimeIT | 20 |

新 PG 套件使用真实 PostgreSQL、由 TaskFixture 驱动的生产服务、MemoryService 和 SDK 执行；画像开启，证据提取关闭。waiting 准备使用 `ScriptedRuntime` 提供合成 Model，沿用实际 AgentScope 执行流程，不是实际模型供应商验收。两个错绑负例保留外键与唯一约束，只在隔离夹具故意修改回执关联，核对当前和另一 queued Command 的前后事实均不变。

```bash
scripts/mvn.sh -B -ntp -o validate spotless:check package \
  -Dit.test=RecoveryReceiptQueryIT,TaskLifecycleIT,ExecutionHeartbeatIT,RecoveryIT,FeishuIT,WorkingContextIT,WorkerClaimQueryIT,FeishuDeliveryQueryIT,ExecutionPrivacyIT,PrivateAgentRuntimeIT \
  failsafe:integration-test failsafe:verify
```

本阶段没有重跑其余集成、脚本用例、Compose 或远程 CI。提交前的静态交付检查另行执行；前置远程 1,843／46／44 的[原始证据](evidence/p3a10-remote-ci.json)保持独立。夹具按各自编译工件建立有效发布，不证明跨工件版本继续旧执行。

## 剩余热点

- Outbox 批量投影仍有逐条读取，需要先补并发、锁等待和规模基线；绑定锁与后续 Turn 新快照读取的先后不能直接合并。
- 通知队首竞争、候选扫描计划与索引仍待测；本阶段不改变通知事务或不确定投递处理。
- Worker 领取后 Turn 回读可另行评估真实 RETURNING，需要连同状态迁移和事件原子性单独验证。
- 多用户连接池等待、心跳续租延迟与租约余量，以及长工具链正文写入字节仍需专项测量。
