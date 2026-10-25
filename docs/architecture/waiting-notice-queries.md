# 等待通知交互查询：P3a.15

**状态：已推送 `36459c4`，远程完整 CI 通过。** [run 40193745424](https://github.com/westbrook0904/FinanceClaw/actions/runs/40193745424) 整条 completed/success：1,320 单元 + 574 集成 = 1,894 Java、46 脚本、44 部署检查通过。[远程证据](evidence/p3a15-remote-ci.json)单列，原提交前 [1,497 项本地验收](evidence/p3a15-implementation.json)及过程保持。

## 当前实现

waiting 通知原先读取整个 Turn 的交互历史，FeishuText 再丢弃非 pending 行，导致每次投影重复映射旧回答。现在 [GovernanceMapper](../../src/main/java/com/financeclaw/infrastructure/governance/GovernanceMapper.java) 新增 `listPendingInteractionsByTurnId`，只增加 `status='pending'` 条件；共享原完整 18 字段、`expires_at>clock_timestamp() AS live` 和 `ORDER BY revision,id`。

[执行上下文端口](../../src/main/java/com/financeclaw/app/execution/port/ExecutionContextGateway.java)及 [MyBatis 实现](../../src/main/java/com/financeclaw/infrastructure/task/MyBatisExecutionContextGateway.java)提供通知专用 pending 读取，[FeishuDelivery](../../src/main/java/com/financeclaw/channel/FeishuDelivery.java) 只在 waiting 分支调用。原 `interactions`、`listInteractionsByTurnId` 仍返回完整历史，供 API、检查点与原生 SDK 恢复使用；没有兼容 fallback 或默认过滤实现。

过期 pending 仍由 SQL 返回并带 `live=false`，原渲染器继续过滤。binding 锁后的独立 owner/Turn 查询、状态/revision 校验、逐 binding 提交、投影 UPSERT、通知领取和事务外发送保持。没有改变 schema、索引、授权、队列、轮询、恢复或心跳。

## 数据与测量来源

[WaitingNoticeQueryIT](../../src/test/java/com/financeclaw/p3/WaitingNoticeQueryIT.java) 使用真实 PostgreSQL 和 TaskAgentRuntime，主场景的测试模型固定生成三批双审批调用；每批通过正常 ASKING、waiting 封存、AnswerInteractionService 回答及 Worker 恢复。旧回答的大 reason 为 1,995 个 UTF-16 code unit，处于原 2,000 字符和 16,384 字节上限内；不以测试 SQL 伪造回答、交互或执行状态。只调整过期场景的真实问题 expires_at，并为回滚场景注入隔离的 PG trigger 故障。

部分审批后 Turn/Command/attempt 不变且仍等待；全部回答后正常完成，累计 6 份报告、4 次 attempt。实际持久调用计数不超过原发布的 12 模型/32 工具预算。测试同时核对有效审批 token、文本排序、过期 token 拒绝、双 owner 路由、取消后的 invalid 历史、投影回滚及重试幂等。正常多轮形成 answered+pending；invalid 在真实取消后验证，不伪造同一 Turn 的 invalid+pending 状态。测试模型使用固定脚本响应，末端 AgentScopeFeishu.send 为替身，未验证远端模型或飞书原生 HTTP SDK。

[WaitingInteractionRecorder](../../src/test/java/com/financeclaw/p3/WaitingInteractionRecorder.java) 仅观察两个精确 MyBatis statement 的四参数 Executor.query 返回集合，统计已经映射的记录、固定状态、pending live/expired 数量，以及 response 字符串的 UTF-8 重编码长度；原样返回集合，不增加 SQL 或 JDBC getter。每个窗口的观察序列与 JDBC interaction_read execute 摘要逐项一致，取消窗口要求二者均为零；缓存返回和重复注册不能冒充实际查询收益。原始报告没有正文、路由、owner 或交互标识值。

窗口只含生产 `project` 调用；绑定、受理、原生模型/工具执行、全量历史 oracle、发送、回答与恢复均在窗口外。正文长度是**映射后逻辑长度**，与上一阶段候选 getter 指标不同；不代表 JDBC getter、PG wire、TOAST、内存分配、吞吐或扫描页。

## 前后实测

| 投影窗口 | SQL 前→后 | 映射记录前→后 | response 逻辑字节前→后 | commit/rollback |
| --- | --- | --- | --- | --- |
| 多轮历史及当前批次部分回答 | 4→4 | 6→1 | 22,740→0 | 1/0 |
| 部分 pending 过期 | 4→4 | 4→2 | 9,096→0 | 1/0 |
| 全部 pending 过期 | 3→3 | 4→2 | 9,096→0 | 1/0 |
| 用户 A 的等待通知 | 4→4 | 4→2 | 9,096→0 | 1/0 |
| 用户 B 的等待通知 | 4→4 | 4→2 | 9,096→0 | 1/0 |
| 用户 A 取消后的通知 | 3→3 | 0→0 | 0→0 | 1/0 |
| 投影插入失败并回滚 | 4→4 | 4→2 | 9,096→0 | 0/1 |
| 移除故障后重试 | 4→4 | 4→2 | 9,096→0 | 1/0 |
| 已有通知再次投影 | 4→4 | 4→2 | 9,096→0 | 1/0 |
| 合计 | **34→34** | **34→15** | **86,412→0** | **8/1** |

8 次通知交互查询由 34 条记录（19 answered、15 pending）缩为 15 条 pending，其中 live 12 条、expired 3 条保持。回滚、重试和重复投影各自计量，合计不是唯一交互数量。通用历史、完整通知正文与路由通过真实 JUnit 断言验证；原生恢复仍需已回答历史，不在此阶段过滤。

原候选 7 窗口保持 85 SQL/23 commit/0 rollback，NoticeClaim 16 窗口保持 244/54/1，旧 FeishuDelivery 25 窗口保持 345/78/0。严格比较仅允许新窗口 8 处与旧 Feishu 1 处交互模板替换，其他 SQL 顺序、连接/线程分组、事务结果、固定业务事实及既有候选/锁内正文指标保持。

## 验证与产物

先在 `e115834` 原生产源码上运行 4 套、26 项 PG 基线；随后应用 4 个生产文件和旧 Feishu 测试一处 statement 元数据修改。两份新测试/观察器、旧通知夹具和计数器前后摘要保持。原三个报告的历史 mode=after 标签保留，fresh before 身份由 runner、Git blob、源码摘要、日志和 XML 确认。

最终同次命令：

```bash
scripts/mvn.sh -B -ntp -o validate spotless:check package -Dit.test=WaitingNoticeQueryIT,NoticeCandidateQueryIT,NoticeClaimQueryIT,WorkerClaimQueryIT,TaskLifecycleIT,ExecutionHeartbeatIT,RecoveryIT,FeishuIT,WorkingContextIT,RecoveryReceiptQueryIT,FeishuDeliveryQueryIT,ExecutionPrivacyIT,PrivateAgentRuntimeIT,ExecutionBudgetQueryIT,TurnDatabaseBaselineIT failsafe:integration-test failsafe:verify
```

实际 XML：**1,320 单元 + 15 套选定集成中的 177 项 = 1,497 项**，失败/错误/跳过均为零，本阶段 Maven 构建与测试无失败尝试。468 项输入无漂移，419 个生产 class、22 个资源（包含三份根目录许可证）与本次 JAR 字节一致；JAR SHA-256 为 `a06cdb77cd5e77008087547121302c11b69a5a6099d785051d6a3beed4f2839d`。证据保留前后四份原始报告、严格比较及其合成负例、命令、真实退出、源码和产物指纹。比较器的合成自测不作为业务验收。证据生成器首次因要求 test-compile 基线存在单元报告目录而拒绝生成；仅修正生成器后通过，原始报告和源码未改，过程记录在 evidence_generation_notes。

原提交前本地命令未执行完整项目 verify、脚本测试套件、Compose、远程 CI、独立 CLI 或部署后 JAR 验收；后续完整远程结果单独记录于文首，不改写原本地范围。

## 剩余热点

交互表当前没有本查询的专用 turn/status 索引，扫描、排序和索引写入成本需用规模 EXPLAIN/BUFFERS 单独评估。回答完成判断已由 [P3a.16](interaction-completion-queries.md) 改为同 Turn/attempt 布尔探测并完成本地验收；原生恢复继续保留所需回答。Outbox 批量投影、队首竞争、恢复扫描、多用户连接池等待及心跳续租延迟仍见 [后续清单](worker-outbox-queries.md#后续五类数据库热点)。
