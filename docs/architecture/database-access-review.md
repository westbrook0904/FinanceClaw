# 当前数据库访问策略

数据库仍是任务、权限、结果和隐私的权威来源。优化目标是减少重复读取和无工作扫描，不取消执行围栏或隐私核验。

| 链路 | 当前策略 |
| --- | --- |
| SSE | 先订阅再持久回放；PostgreSQL LISTEN/NOTIFY 唤醒跨实例持久事件补读，临时文本/工具增量仍在执行 JVM 内；空闲 keepalive 不扫描消息表 |
| Task Worker | 提交后本地唤醒、空闲退避、过期恢复独立调度 |
| 执行心跳 | 单 SQL 续租、专用调度与预留 Hikari 资源；token、锁后数据库时间及本机注册截止点共同阻断过期执行 |
| Memory Worker 领取 | 一条 CTE 锁定候选并更新租约，保留 SKIP LOCKED |
| 记忆租约校验 | 锁后检查当前 token、owner、epoch、revision、模型版本与数据库时间 |
| 同用户并发受理 | 已存在的 owner 使用共享锁；画像编辑和遗忘持独占锁，等待受理事务结束 |
| 授权幂等重放 | 一次查询读取请求摘要与原回执 |
| 报告发布 | 一次事务 INSERT 同时保存实体与精确回执；没有不完整报告状态或全局兼容扫描 |
| 飞书投递 | 空闲退避至五秒，恢复独立扫描；未知或未验证送达不自动重投 |
| 运行管理 SQL | worker 注册、工件目录和用量查询复用 Boot 托管的 JdbcTemplate，继承统一模板配置；不另建默认模板绕过 query-timeout 等属性 |

## 事务由谁管理

Spring `TransactionTemplate` / `JdbcTransactionManager` 与 MyBatis-Spring 已负责连接绑定、提交、回滚和异常转换。业务层保留短事务的边界、锁序、lease/fence、授权与隐私检查、检查点 CAS 和封存回执，不另外实现事务框架。模型、MCP、embedding 与飞书网络请求继续在数据库事务外执行；普通 JDBC 事务不会随 Reactor 的线程切换传播，因而不能用包住整个 `Flux` 的事务注解替代实际写入线程中的短事务。

事务回调内的业务异常会请求回滚，但提交阶段连接中断可能无法判定最终结果，`afterCommit` 回调异常也不代表数据库已回滚。执行器把 `seal` 与 `finish` 分成两个提交点：已封存结果在后续收尾失败后仍可由恢复器读取；不能重新调用模型，也不能根据网络异常补写相反的终态。恢复每次重新读取持久 command/receipt，未知外部效果继续阻断。

本地唤醒和事件失效直接使用 Spring `TransactionSynchronization`，提交后只更新内存状态或向有界队列入队，不借数据库连接或发网络请求。撤权对象在原授权锁内捕获。当前不机械改成异步事件监听，也不加入通用自动重试：事件投递顺序、事务提交事实与外部效果的幂等不是同一个问题。

## 复用现有组件

| 调整 | 实际收益与边界 |
| --- | --- |
| 复用 Boot 配置的 `JdbcTemplate` | 运行管理适配器不再自行 `new JdbcTemplate(DataSource)`；`spring.jdbc.template.*` 配置能够到达其实际 SQL 语句。连接仍来自原业务 DataSource，参与原调用者事务，不减少 SQL 或增加连接池 |
| Spring `ConcurrentLruCache` + `CronExpression` | 每个 cadence 实例最多缓存 256 个编译表达式，减少重复解析。时区、起点与下一次发生时间每次重新计算；不缓存规则、权限、版本或数据库时间 |
| Jackson 不可变 `ObjectReader` | 最终模型请求检查复用固定的解析配置，避免每次创建 ObjectMapper。不缓存请求正文或解析树，各模型容量与输出预留仍独立校验；不使用 HTTP DTO 定制过的 mapper |

运行管理 SQL 可通过 Boot 标准属性设置查询超时，例如：

```yaml
spring:
  jdbc:
    template:
      query-timeout: 5s
```

该属性控制使用此模板的 JDBC 语句；MyBatis 语句与专用心跳另有自己的超时配置，不能把它当成所有数据库访问的统一截止时间。已有 Spring 事务期限的应用规则仍交给框架。LISTEN 的独占连接与心跳的专用资源有不同生命周期，继续保留其局部模板和事务管理器。

当前版本与依据：[Boot 3.5 JDBC 配置](https://docs.spring.io/spring-boot/3.5/reference/data/sql.html)、[Spring 6.2 编程式事务](https://docs.spring.io/spring-framework/reference/6.2/data-access/transaction/programmatic.html)、[Spring 有界缓存](https://docs.spring.io/spring-framework/docs/6.2.x/javadoc-api/org/springframework/util/ConcurrentLruCache.html)、[Jackson 2.21.4 ObjectReader](https://github.com/FasterXML/jackson-databind/blob/jackson-databind-2.21.4/src/main/java/com/fasterxml/jackson/databind/ObjectReader.java)。本轮未新增依赖，也未更改 SQL、数据库结构、事务传播策略或自动重试行为。这些是配置一致性和对象复用优化，没有据此宣称吞吐提升或生产容量。

## 验证范围

`RuntimeJdbcConfigurationTest` 使用真实 Boot 自动配置与 JDBC 记录桩，检查非默认 timeout/fetch-size/max-rows 确实传到三个适配器的语句，并检查同一事务复用连接；它不证明 PostgreSQL SQL 或锁语义。`TriggerCadenceTest` 检查不同起点、时区、DST 与非法输入；`ModelPolicyTest` 检查同步/流式请求在网络前拒绝非法正文。真实数据库、运行装配与恢复仍由 `RuntimeCoordinationIT`、`RoleAssemblyIT` 及完整 CI 验收。

2026-10-27 本轮首次统一本地验收：**1,430 项单元测试、53 项 Python 契约测试全部通过**，失败、错误、跳过均为 0；静态检查、Spotless 与打包通过，605 个构建输入在运行前后保持一致。[验收记录](evidence/docs-components-local-validation.json)保存实际命令、输入与 JAR 摘要。本机 Docker API 五秒探测仍超时，因此该记录不代表本地 PostgreSQL、Compose 或完整 verify 通过。

首次远程提交 `7ff294b` 的 [Actions 40434256555](https://github.com/westbrook0904/FinanceClaw/actions/runs/40434256555) 通过 2,121 Java（1,430 单元 + 691 集成）及 53 脚本检查；部署在前 20 项通过后，因独立 SQL 已恢复但业务 HTTP 首次读取仍为 503 而失败，不能称为完整 CI 成功。后续修复只收紧[恢复探测边界](runtime-coordination.md#验收入口)，保留原失败与通过的用例，不更改生产事务、TTL 或恢复阈值。修复后本地五组 Python 契约 **59 项与静态检查通过**；Java 源码、资源和构建配置仍与已通过的打包运行一致，未把分次结果声称为新的本地完整 verify。最终完整结论以修复提交对应 Actions 为准。

以上策略来自代码路径分析，不是生产压测结论。[预算查询优化](execution-budget-queries.md)已通过真实 JDBC 对比验证固定 v3 发布场景中同次扣额减少四次发布读取；后续[整轮数据库基线](turn-database-baseline.md)记录普通 Turn、行情父子 Turn 和双 owner 四会话的跨线程 SQL，使用本机模型协议服务及明确的测试调度参数。连接池容量、真实模型耗时、多主体生产负载与实际查询计划仍需按真实环境测量。历史分析及当时的计数保存在[历史记录](../history/architecture/database-access-review.md)。
