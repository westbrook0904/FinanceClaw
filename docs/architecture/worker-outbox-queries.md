# Worker 与渠道 Outbox 查询优化：P3a.10

**状态：2026-10-25 已推送 `06ec674`，远程完整 CI 通过。** 前置生产版本为本地提交 `0c789b8`；本阶段优化领取与通知的重复数据库访问。
原生产 before 的 19 项真实 PostgreSQL 基线通过；最终 after 同次通过 **1,299 单元 + 113 选定集成 = 1,412 项**，零失败、错误、跳过，Maven exit 0。
原始窗口、SQL 严格比较、报告摘要及输入指纹见[本阶段验收证据](evidence/p3a10-implementation.json)。

## 实现范围

### Worker：使用数据库返回的 Command

`ExecutionLeaseService.claim` 仍先以 `FOR UPDATE SKIP LOCKED` 取得 queued Command，再锁 Turn 并检查其可领取状态。
`TaskMapper.updateCommandClaim` 将原 UPDATE 改为 `UPDATE ... RETURNING`，返回数据库实际写入的 attempt、fence、live 和 resumeId。
`MyBatisCommandGateway` 原样传递返回行，不用旧 Command 或 Java 时间合成租约元数据；同事务中不再回读已经持锁的 Command。
Mapper 显式设置 `affectData=true`、刷新缓存和禁用查询缓存；空返回立即抛 `Fenced`，不得继续创建 attempt 或迁移 Turn。
创建 attempt、Turn 迁移、持久事件与事务提交保持原子性，空队列仍只执行一次候选查询。
后续执行仍重新锁 Command 校验当前 attempt、fence 和数据库租约，不复用领取时的 live 布尔值代替执行门禁。

### Outbox：减少无用途读取并处理有限积压

只有 `waiting` 文案需要交互列表；completed、blocked、failed、cancelled 使用最终结果或固定提示，不再读取 interactions。
内部领取仍按 binding → notice 加锁，检查 pending、muted、activeTurn、binding revision、owner/conversation 和 Turn revision。
领取结果同时携带该次已锁 binding 的 chatId，省去原发送前仅为获取路由的 binding 回读及独立短事务。
此路由只在同次领取中使用，不是授权缓存；领取后绑定变化能否撤回外部发送，仍遵循原有发送资格边界。
每轮最多检查 20 个候选，已抑制或被其他执行者领取的候选算已处理工作，避免误判为空队列并不断延长退避。
取得有效候选后每轮最多调用一次外部发送；公开 `claim()` 仍只检查一次候选，遇旧项抑制后返回 null。
网络调用在领取事务之外，成功只记 `submitted_unverified`，失败或失联记 `unknown`，均不自动改回 pending 重发。

### NOTICE：提交后缩短本进程退避

`WorkSignals.Queue.NOTICE` 使用独立 AtomicLong，与 COMMAND 互不推进；代次不保存 owner、正文、权限或发送身份。
Mutation 从真实 `INSERT ... RETURNING Event` 读取状态，仅 waiting、blocked、completed、failed、cancelled 安排提交后唤醒。
Feishu 新 inbox 回执和有效通知投影提交后推进 NOTICE；旧入站事件回放直接返回原回执，回滚不推进代次。
`scheduled()` 在查询前取得当前代次票据；空扫描期间有新提交时，下一 tick 观察到代次变化即可清除旧退避。
流式 token/tool 增量不触发通知数据库扫描；重启、跨实例提交和本地信号缺失仍由低频持久扫描兜底。
恢复扫描独立调度，不能因通知退避而停止检查已失联的 sending。

## JDBC 比较方法

WorkerClaimQueryIT 和 FeishuDeliveryQueryIT 通过真实 PostgreSQL、生产 Mapper/服务和 JDBC execute 计数建立前后样本。
准备数据、模型执行、断言及观察查询位于窗口之外；并发 Worker 各在线程自己的观察窗口计数，不混成逐 owner 全链路成本。
Feishu 用例仅替换最末端 `AgentScopeFeishu.send`，不在该套件声称认证原生 SDK HTTP；既有 FeishuIT 单独回归该边界。
下表为已封存 before/after 的实际 JDBC execute 次数，不是静态估计；准备与验证查询不计入这些窗口。

| 样本 | before SQL | after SQL |
| --- | ---: | ---: |
| 成功 Worker 领取 | 8 | 7 |
| 空 Worker 队列 | 1 | 1 |
| completed 首次投影并发送 | 12 | 10 |
| waiting 首次投影并发送 | 12 | 11 |
| completed 显式维护 poll | 13 | 11 |
| completed 单独投影 | 4 | 3 |
| 已持久化 pending 的发送 | 8 | 7 |
| 20 个绑定投影及一次发送 | 88 | 67 |
| 随后 2 个绑定投影及一次发送 | 16 | 13 |
| 双 owner 剩余 20 条 pending 排空 | 160 | 140 |

普通窗口比较有序 family/template 摘要与事务连接事实，仅允许删除约定的非 waiting 交互读取及独立路由回读。
Worker 只允许 UPDATE 模板替换成约定的 RETURNING 形式，并删除后续 Command 回读；其余业务语句及事务结果实际保持。两个竞争 Worker 的赢家允许互换，但必须仍只有一个成功 attempt。
旧通知比较采用同一完整工作量：21 条过期绑定通知和 1 条有效通知，不把不同轮数的中间窗口硬当等价样本。
实测旧实现需要 22 次维护 poll，合计 135 条 SQL；新实现两轮 82+12=94 条，22 次候选的安全 SQL、有序模板及连接分组保持，提交数从 23 降至 22（移除独立路由事务）。
新实现第一轮只抑制 20 条，第二轮抑制第 21 条再发送有效项；减少的维护扫描和路由读取不能写成单候选扣额减少。
该对比不证明跨 owner 轮流服务、生产吞吐、网络往返时延或任意比例的数据库容量提升。

## 验收与过程记录

| 执行 | 数量与结果 | 范围 |
| --- | --- | --- |
| before | 19 项真实 PG，exit 0 | 原 `0c789b8` 生产；修正后的新基线 fixture |
| after 单元 | 1,299 项，零失败/错误/跳过 | 源码冻结后的全量单元 |
| after 选定集成 | 113 项，零失败/错误/跳过 | 下列 9 个套件，最终同次命令 exit 0 |
| SQL 严格比较 | 通过 | Worker 13 个窗口、Outbox 22 个普通窗口及 3 个完整积压窗口；原始 SHA-256 与模板比较保存在证据 |
| 打包字节校验 | 417 class、22 资源全部匹配 | 459 项构建输入在执行前后不变；3 份许可证按 JAR 根 META-INF/licenses 路径核对 |
| 静态交付检查 | 文档链接、语法、凭据模式、空白检查通过 | `python3 scripts/check-delivery.py` |
| 脚本用例、Compose、远程 CI | 提交前本地未重跑；后续远程成功见下文 | 原本地范围与后续远程结果分别记录 |

9 个套件为 WorkerClaimQueryIT 9、FeishuDeliveryQueryIT 10、TaskLifecycleIT 25、ExecutionHeartbeatIT 14、RecoveryIT 13、FeishuIT 12、WorkingContextIT 20、TurnDatabaseBaselineIT 3、ExecutionBudgetQueryIT 7。
FeishuDeliveryQueryIT 的双 owner 22 绑定验证有限积压最终发送到正确 chat，不证明轮流公平调度。Worker resume 用例验证合法绑定元数据下领取，不代替真实 SDK 的审批恢复验收。

最终通过命令：

```bash
scripts/mvn.sh -B -ntp -o validate spotless:check package \
  -Dit.test=WorkerClaimQueryIT,FeishuDeliveryQueryIT,TaskLifecycleIT,ExecutionHeartbeatIT,RecoveryIT,FeishuIT,WorkingContextIT,TurnDatabaseBaselineIT,ExecutionBudgetQueryIT \
  failsafe:integration-test failsafe:verify
```

本历史阶段的 before 使用 `-Dfinanceclaw.test.query-baseline=true test-compile`，仅选择两个新 IT；生产输入与 `0c789b8` 的 278 项文件逐个核对一致。
成功 before 后，Feishu 测试仅因构造器新增参数而接入共享 WorkSignals，Worker fixture 保持；不把两套测试源码表述为字节完全相同。
CI 原有 `target/p3*/` 归档规则覆盖 `target/p3a10/` SQL 报告。P3a.12 已为 Worker 改用专用 7→6 基线开关和 `target/p3a12/worker-turn-returning-sql.json`，旧开关不适用于当前 Worker 测试。P3a.13 又为当前 Feishu 回归移除该历史开关，新增 NoticeClaimQueryIT 专用基线；当前输出见 `target/p3a13/`。

两次 Docker API 启动中断均发生在业务集成执行前：before 为 0 用例；首次 after 已通过 1,299 单元、0 集成，均主动中止退出 143。原日志、报告及输入指纹保留，不混入最终同次成功结果。
恢复 Docker 使用用户此前的明确授权；不把本轮中断挪用为其他阶段的失败历史，也不在无证据时认定所有失败均由环境导致。
一次 Worker resume 测试 fixture 违反外键，修正测试引用关系后重跑；没有为测试修改生产逻辑、删除外键或放宽数据库约束。
最终记录保留上述原始尝试、源码和资源输入指纹、JAR 摘要、报告来源及真实退出；本阶段最终选定范围同次通过，但没有执行全项目完整 verify 或 Compose。
执行心跳继续保留原双锁 CTE、锁后数据库时间、取消观察及 attempt/fence/租约校验，本阶段没有减少续租或放宽未知执行恢复。

## 后续五类数据库热点

1. **Outbox 批量投影仍逐条查询。** 目前先列最多 20 个 binding，再逐项锁定/读取/插入。后续先测 1/20/多 owner 批次与重绑、静音、修订并发，再考虑集合读取和 activeTurn 索引；不能把发现结果当授权缓存。
2. **通知队首竞争。** 多个执行者可能先观察同一 pending；需要多实例、旧通知前缀的 SQL 与锁等待证据，再评估保持 binding → notice 锁序的有界领取和 SKIP LOCKED。
3. **执行事务的后续改进。** 原阶段发现的恢复 sealed receipt 重读已由 [P3a.11](recovery-receipt-queries.md) 消除；Worker running 后回读由 [P3a.12](worker-turn-returning-queries.md) 使用真实 RETURNING 消除并完成本地验收。事件、完整字段、sealed/fence/checkpoint 和回滚语义保持，历史 8→7 SQL 数据不重写。
4. **恢复扫描的规模成本。** 命令过期和 sending 失联扫描仍需大表 EXPLAIN/BUFFERS，再决定部分索引与有界扫描；锁后仍要用数据库时间重新检查，unknown 不自动重发。
5. **多用户连接池与心跳延迟。** 当前心跳已是单条双锁 CTE；重点测 Hikari 等待、锁等待、续租延迟及租约余量，按证据评估资源隔离，不靠降低心跳频率、缓存 live 或批量续活减 SQL。

[P3a.13](notice-claim-lock-queries.md) 已推送 `dda1d7d` 并通过远程完整 CI，领取时双锁查询合并，完整旧通知积压进一步 94→72 SQL。[P3a.14](notice-candidate-results.md) 已推送 `e115834` 并通过远程完整 CI，候选 8→2 列、去除候选正文 getter 重复读取，SQL 次数保持不变。两者保留逐项投影事务和原队首等待，批量投影与公平性仍未完成。

[P3a.15](waiting-notice-queries.md) 已推送 `36459c4` 并通过远程完整 CI，waiting 通知使用 pending 专用读取，9 个投影窗口映射 34→15 行、旧回答逻辑字节 86,412→0，SQL 次数保持。原生恢复继续按需读取完整交互历史；[P3a.16](interaction-completion-queries.md) 已本地验收回答完成判断的同 Turn/attempt 布尔探测，7 个实际回答窗口 response 逻辑字节 131,892→0，SQL 次数保持，尚未推送。

长工具链的 SDK/工作上下文写入字节、重复正文和写入放大仍需独立测量；本阶段的 SQL 次数不能替代这些证据。

实现入口为 WorkSignals、ExecutionLeaseService、TaskMapper、FeishuService 和 FeishuDelivery；测试入口为 WorkerClaimQueryIT、FeishuDeliveryQueryIT、FeishuIT 及执行生命周期/心跳回归。
本阶段保持单业务 JVM、现有 41 表和原持久权威，不新增任务队列、子心跳、冗余通知表或轮询来源。

## 远程交付补充

[run 40173421896](https://github.com/westbrook0904/FinanceClaw/actions/runs/40173421896) 针对 `06ec674` 整条 completed/success：**1,299 单元 + 544 集成 = 1,843 项 Java、46 项脚本及 44 项部署检查**通过，覆盖前置发布合并与本阶段 Worker/Outbox 改动。原本地 1,412 项选定范围和中断记录不改写；[远程证据](evidence/p3a10-remote-ci.json)单独保存，后续 P3a.11 不在此远程运行范围内。
