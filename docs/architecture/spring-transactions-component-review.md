# Spring 事务与现成组件复审

复审日期：2026-10-15；R5a 实施更新：2026-10-16。承接 [AgentScope 与 COLA 整改方案](agentscope-cola-remediation.md)，修正上一轮对事务接入和组件引入过于保守的判断。

## 1. 更新后的决策

**可以、也应该由 Spring 统一管理本工程的本地数据库事务。当前 MyBatis 生产路径实际上已经这样实现，不需要再引入 AgentScope 的 JDBC 事务管理。**

需要分清三件事：

| 层面 | 应采用的实现 | 本项目负责的内容 |
| --- | --- | --- |
| 事务生命周期 | Spring `JdbcTransactionManager` / `TransactionTemplate` / `@Transactional` | 确定哪些业务变更必须在同一事务中 |
| SQL、连接与映射 | 现有 MyBatis-Spring；新增少量独立 SQL 可直接用 Spring `JdbcClient` | 业务条件 SQL、表结构、唯一约束 |
| Agent 状态接口 | AgentScope `AgentStateStore` + 薄业务适配 | namespace、owner、墓碑、当前 command/attempt/fence 校验 |

`JdbcAgentStateStore` 是一个带 JDBC 持久化和内部提交行为的具体实现，**不是使用 AgentScope 时必须采用的事务系统**。复用 SDK 的接口、状态格式和执行器，同时用 Spring 管事务，完全成立。

修正后的原则：先使用成熟组件完成通用技术职责；只在稳定的业务边界上增加适配。不能因为需要业务治理，就默认保留或扩建一整套自研基础设施。反过来，引入组件也不能只看名字相似而忽略实际提交行为。

## 2. 当前项目已经使用 Spring 事务

[pom.xml](../../pom.xml) 通过 `mybatis-spring-boot-starter:3.0.5` 引入 Spring JDBC/事务组件；当前 Boot 3.5.16 管理的 Spring Framework 是 **6.2.19**。

[ExecutionLeaseService.fenced](../../src/main/java/com/financeclaw/app/execution/ExecutionLeaseService.java) 承担原 TaskService 的租约事务，实际逻辑是：

```java
return tx.execute(status -> {
    current(claim);   // 锁住并核对当前 command / attempt / fence
    return action.get();
});
```

[TaskAgentRuntime](../../src/main/java/com/financeclaw/infrastructure/agentscope/execution/TaskAgentRuntime.java) 把状态 `saveIfVersion` 放入这个边界；[MyBatisAgentStateStore](../../src/main/java/com/financeclaw/infrastructure/state/MyBatisAgentStateStore.java) 只调用 Mapper，没有自行 `getConnection/commit/rollback`。MyBatis-Spring 的会话会参与 Spring 事务，事务管理器与 SqlSessionFactory 必须使用同一个 DataSource。[MyBatis-Spring 事务说明](https://mybatis.org/spring/transactions.html)。

因此，现在不需要“再写一个 Spring 事务框架”，也不需要把所有 Mapper 改写成原生 JDBC。可以保留这个正确基础，把目前分散在应用大类和匿名子类中的**业务保护职责**整理到明确的执行/状态写入服务中。

## 3. 为什么原生 JDBC 库不能只加一个注解或代理

### 3.1 固定版本的源码行为

AgentScope 2.0.3 的 `JdbcAgentStateStore.executeInWriteTransaction` 无论传入连接原先是否为 `autoCommit=false`，执行成功后都会调用 `conn.commit()`，失败后会调用 `conn.rollback()`。它没有“已有外部事务就不提交”的分支。[固定版本源码](https://github.com/agentscope-ai/agentscope-java/blob/v2.0.3/agentscope-extensions/agentscope-extensions-jdbc/src/main/java/io/agentscope/extensions/jdbc/state/JdbcAgentStateStore.java)。

Spring `TransactionAwareDataSourceProxy` 能让普通 JDBC 代码拿到线程绑定的事务连接，并正确处理 `close()`。它**不是禁止手工提交的连接包装器**；其代理会把未特殊处理的方法继续委托给底层连接，包含 `commit()`。[6.2 API](https://docs.spring.io/spring-framework/docs/6.2.x/javadoc-api/org/springframework/jdbc/datasource/TransactionAwareDataSourceProxy.html)。

两种配置的区别：

| 配置 | 实际风险 |
| --- | --- |
| 外层 Spring 事务 + 原生状态库使用普通 DataSource | 原生库另取连接、独立提交，外层回滚不能撤销状态保存 |
| 外层 Spring 事务 + 原生库使用事务感知 DataSource | 共享了连接，但原生库主动 commit 会提前提交同连接上此前的业务 SQL |
| Spring 事务 + MyBatis-Spring / JdbcClient | 由 Spring 控制统一提交和回滚；适合当前单库短事务 |

所以，上一轮“可以以后实验事务感知代理”的说法需要收窄：**已经验证单加该代理不能解决 2.0.3 的主动提交问题，不应作为候选生产方案。** 这与“Spring 能否管理 JDBC 事务”是两个不同问题，后者的答案明确是能。

### 3.2 本轮实际事务实验

2026-10-15 复审时 Docker daemon 未运行，未启动或修改现有数据库服务。使用项目已有 SQLite JDBC 驱动，在独立临时数据库中运行真实 Spring/MyBatis/AgentScope JDBC 代码；实验结束删除临时数据库。没有用 mock 模拟提交结果。

| 实验 | 观察到的结果 | 判断 |
| --- | --- | --- |
| MyBatis 与 JdbcClient 同一 Spring 事务后抛异常 | 两条写入均不存在 | Spring 能统一回滚两种数据访问方式 |
| MyBatis 与 JdbcClient 同一 Spring 事务正常结束 | 两条写入均存在 | Spring 能统一提交 |
| 原生状态库使用普通 DataSource，外层事务回滚 | SDK 状态仍在，外层业务写入不存在 | SDK 写入没有加入外层事务 |
| 原生状态库使用事务感知代理，保存前后均有业务 SQL，最终回滚 | SDK 状态和保存前的业务 SQL 仍在；保存后的业务 SQL 回滚 | SDK commit 提前提交了外层已有工作 |

四项均符合预期，完整数据见 [事务复审证据](evidence/spring-transactions-review.json)，源码见 [SpringTransactionProbe.java](experiments/SpringTransactionProbe.java)。这些结果验证了 JDBC 提交边界，**不代表 PostgreSQL 的行锁、fence、CAS 竞争、隐私或恢复验收通过**。

### 3.3 推荐实现方式

默认继续采用 Spring 管理事务 + 现有 MyBatis 状态适配。如果未来改用 `JdbcClient`，只改变数据访问实现，保留同一个 Spring 事务所有者。没有必要为了使用 `JdbcAgentStateStore` 而维护一个拦截 commit/rollback 的自定义 DataSource。

若今后 SDK 提供明确的 Spring 事务模式或允许调用方传入事务执行策略，可以重新评估原生状态库；它不再是当前架构整改的前置事项。原生状态库自身的 CAS 仍然值得复用其设计，但当前 Mapper 已使用条件 SQL 完成 CAS，不存在需要另造通用 CAS 引擎的问题。

## 4. `@Transactional` 与 `TransactionTemplate` 如何选

| 调用场景 | 推荐 | 具体约束 |
| --- | --- | --- |
| 同步的受理、回答、取消、完成用例 | Spring Bean 的 `@Transactional`，或现有 `TransactionTemplate` | 保持锁顺序、传播方式和异常回滚契约；经 Spring 代理调用 |
| SDK 状态回调、fence 包围的小段持久写入 | `TransactionTemplate` | 在实际执行数据库操作的线程中进入事务，边界直观且不依赖自调用代理 |
| LLM、远程 MCP、embedding、飞书发送 | 事务外执行 | 调用前后各自短事务核对，不持有数据库锁等待网络 |
| Reactor `Mono/Flux` 包围阻塞 MyBatis | 在阻塞操作执行处进入上述短事务 | JDBC 事务线程绑定；给返回 Mono 的方法加普通事务注解，不等于订阅时数据库操作受保护 |
| 新增少量独立查询/更新 | `JdbcClient` / `JdbcTemplate` | 与 MyBatis 使用同一个 DataSource；避免同一能力同时维护两套 SQL |

Spring 的注解事务默认通过代理生效，类内部自调用不会自动经过代理；默认回滚规则也不覆盖所有 checked exception。需要全异常回滚的用例应明确配置，不能在把 Template 改为注解时悄悄改变契约。[Spring 6.2 注解事务](https://docs.spring.io/spring-framework/reference/6.2/data-access/transaction/declarative/annotations.html)。

不机械替换所有 `TransactionTemplate`，不为它再建一个同形的 `TransactionPort/UnitOfWork`。应用层可以直接依赖 Spring 事务；领域层继续只表达规则。需要保留的是具名业务能力，例如“在当前租约下保存快照”，而不是包一层新的通用事务管理接口。

## 5. 组件复用清单与接入决定

以下区分“已接入”“现有组件直接复用”和“与对应切片一起接入”。后者已经给出用途与接入位置，不需要为每个组件再做一轮泛泛的选型；落地时仍要验证对应契约。

| 组件 | 决定 | 替代/减少什么 | 业务边界 |
| --- | --- | --- | --- |
| Spring JDBC / spring-tx **6.2.19** | **已存在，作为统一事务方案** | 手工连接、提交、回滚、异常转换；不新增事务框架 | 原子更新内容、fence、SQL 条件由业务定义 |
| MyBatis-Spring **3.0.5** | **直接复用** | 会话与事务连接管理、SQL 映射 | 不用 SDK 原生 JDBC 再管理一次提交 |
| Spring `JdbcClient` | **当前依赖即可使用** | 新增少量 SQL 时的 JDBC 样板 | 不强制重写现有 Mapper；本轮已验证与 MyBatis 同事务 |
| ArchUnit **1.5.1** | **本轮已引入，test scope** | 手工长期维护 import 检查脚本 | 现有循环依赖必须显式整改；不以通过局部规则宣称全架构无环 |
| `spring-boot-starter-validation` | **静态 DTO 切片优先接入，版本交给 Boot BOM** | HTTP DTO 的空值、范围、长度等重复判断 | 保持未知字段拒绝和错误码；动态工具 Schema 继续用 SDK ToolValidator |
| Jackson `ObjectMapper/ObjectWriter` | **已有，直接复用** | 通用 JSON 编解码和重复 parser 包装 | 为 hash 固定专用 writer 与字节契约；SDK AgentState 继续使用 SDK codec |
| COLA statemachine **5.0.0** | **R4 采用少量领域方法，当前不引入** | 后续需要声明式状态图时采用组件，不自建通用状态机引擎 | 只做转换决策；SQL 锁、事务、恢复、外部效果仍另行处理 |
| Spring Modulith **1.4.x** | **领域边界清晰后评估持久应用事件** | 模块间可靠事件登记、失败记录/重投、模块检查 | 不直接替换受限跨进程 Worker、业务幂等和飞书送达回执 |
| Spring Security / Resource Server | **正式多用户认证切片优先复用** | 不继续把现有单 token Filter 扩成认证框架 | 先明确 JWT/opaque token/身份提供方；tenant/subject 归属和 grant 仍是业务授权 |
| Actuator / Micrometer、Hikari、Flyway | **现有组件继续复用** | 健康/指标、连接池、数据库迁移基础设施 | 只补业务指标与迁移规则，不另造监控/连接池/迁移执行器 |

### 5.1 本轮已经落地的 ArchUnit

只增加 `com.tngtech.archunit:archunit:1.5.1` 的测试依赖，沿用项目 JUnit Jupiter，不引入 JUnit 6 或新的测试执行引擎。官方核心模块包含类导入和规则 API，可直接用现有测试框架调用。[ArchUnit 使用说明](https://www.archunit.org/userguide/html/000_Index.html)。

初次复审增加以下三项检查；当前 [ArchitectureTest](../../src/test/java/com/financeclaw/architecture/ArchitectureTest.java) 已扩展为 11 项，包含新 app、端口、执行入口和已迁移业务适配的依赖门禁：

1. domain 只依赖本领域与 JDK，禁止 Spring/SDK/HTTP/持久化进入。
2. 项目生产类不得调用 `Connection.commit/rollback/setAutoCommit`。
3. API 不得新增 infrastructure 依赖；唯一现存 `OperationsController` 债务单独登记，R2 删除该例外。

第二项不扫描第三方 jar 的实现，也不能证明某段 SQL 没有事务控制，因此不能替代上面的原生 JDBC 实验。第三项没有把整层加入忽略名单，但仍允许登记类保留当前依赖；R2 必须移除例外。R4 已移除旧 agent 包；当前 `application/tools/ziwei` 的存量循环尚未消除。

### 5.2 DTO 校验优先使用 Bean Validation

当前很多 HTTP 入参是手工读取 `JsonNode`。在改为类型化 DTO 时，直接使用 `@Valid`、`@NotBlank`、`@Size` 等成熟机制，无需另建通用验证服务。Boot 的 validation starter 提供所需实现和集成。[Boot 3.5 Validation](https://docs.spring.io/spring-boot/3.5/reference/io/validation.html)。

接入时特别保留：未知字段、字段类型、数值溢出、回答版本和现有 422/409 等错误契约。Bean Validation 只处理结构/静态约束；审批归属、最新授权和 checkpoint 绑定仍由用例/领域规则判断。动态 MCP/tool JSON Schema 继续复用 AgentScope 验证器。

### 5.3 COLA 与 Spring Modulith 可以用，但对应不同职责

已核查 Maven Central 的 COLA statemachine 5.0.0 源码与 POM：它提供 `StateMachine.verify` 和 `fireEvent`。R4 已用 Task/TaskTransition 的少量明确方法整理规则，没有引入转换表组件。后续若需要声明转换表，可直接引入该组件，不必为采用 COLA 增加库。技术状态机装配可放 app 的 transition policy，领域对象保持纯规则；不会把数据库提交塞进 transition action。[固定版源码包](https://repo.maven.apache.org/maven2/com/alibaba/cola/cola-component-statemachine/5.0.0/cola-component-statemachine-5.0.0-sources.jar)。

Spring Modulith 1.4 系列对应 Boot 3.5；其事件登记可以加入原始事务，并保留未完成的发布供重试。这比自建一套通用的持久事件总线更值得优先评估。[兼容矩阵](https://docs.spring.io/spring-modulith/reference/1.4/appendix.html)、[持久事件机制](https://docs.spring.io/spring-modulith/reference/1.4/events.html)。

但当前记忆 Worker 是独立受限进程，飞书发送有 `submitted_unverified/unknown`，并不等于单应用内 listener 成功。适合先用在同一应用边界内、可幂等的后置投影；不能直接删除现有 memory job、通知回执和权限隔离。普通 `@Async`/`@TransactionalEventListener` 也不自动提供持久重试。

### 5.4 撤回不必要的“接口包接口”

上一版建议的端口仍应有选择地建立。`AgentExecutionPort`、外部行情、凭据/发布读取、Artifact 等是真实的变化边界；Spring 事务、Jackson 编解码、Bean Validation 等已有稳定接口的技术组件，不必每种都再做一个项目内同形 SPI。

特别是 JSON：复用一个明确配置的专用 writer，并对 release/action/input hash 留固定样本即可。领域对象接收已经形成的 hash，不依赖 Jackson。不要为了避免应用层认识任何技术类，新增一个只有 `serialize/parse` 转发的“通用 payload 编码框架”。

## 6. 第二轮复审的验证与复现

2026-10-15 复审当时已通过 3 项新架构测试、4 项独立 JDBC 事务实验。生产 Java 和数据库结构没有改变；POM 只新增 test scope 的 ArchUnit。既有 M5e 脚本、SQL、CI 改动保持原样。

架构检查已通过现有 Maven `test` 阶段自动进入 CI，无需另写检查调度器。运行单项检查：

```bash
scripts/mvn.sh -B -ntp -Dtest=ArchitectureTest test spotless:check
```

事务实验使用原版 SDK jar，未把 `agentscope-extensions-jdbc` 加入项目依赖，也没有修改或复制其实现。复现命令：

```bash
mkdir -p target/architecture
scripts/mvn.sh -B -ntp dependency:build-classpath dependency:get \
  -DincludeScope=test -Dmdep.outputFile=target/architecture/probe-classpath.txt \
  -Dartifact=io.agentscope:agentscope-extensions-jdbc:2.0.3 -Dtransitive=false

TX_JAVA="${JAVA_HOME:-$PWD/.tools/jdk/Contents/Home}/bin/java"
if [ ! -x "$TX_JAVA" ]; then
  TX_JAVA="$PWD/.tools/jdk/bin/java"
fi
"$TX_JAVA" \
  -cp "$(cat target/architecture/probe-classpath.txt):.cache/m2/io/agentscope/agentscope-extensions-jdbc/2.0.3/agentscope-extensions-jdbc-2.0.3.jar" \
  docs/architecture/experiments/SpringTransactionProbe.java \
  target/architecture/spring-transactions.json
```

预期输出 `Passed 4 transaction experiments`，其中“SDK 状态在外层回滚后仍存在”是对不兼容接法的反例验证，**不代表这个接法可以投入生产**。

证据文件记录测试数量、版本、实验结果、源码/依赖指纹与验证范围；不引用旧 Surefire/Failsafe 报告宣称全量回归重新通过。该次复审时 Docker 未运行，未执行 PostgreSQL 集成与 Compose 验收。后续 R0–R2 的代码与实测结果另见 [实施记录](r0-r2-implementation.md)。

## 7. 调整后的下一步

R0–R2 已完成第一批实现并通过回归：兼容样本、M0 隔离、共享 JSON/摘要、Operations 分层与移除 Controller 例外，详见 [实施记录](r0-r2-implementation.md)。Operations 继续直接复用 Spring 事务和 MyBatis 查询；严格 JsonNode 输入契约保持原样，不为这次结构调整更换校验方式。R3 已抽取回答用例并沿用 Spring 事务，SDK 校验入口的差异与真实回滚验证见 [R3 实施记录](r3-implementation.md)。R4 已完成任务用例与执行端口分层，继续直接使用 Spring 事务，新增 6 处实际回滚验证，见 [R4 实施记录](r4-implementation.md)。R5a 已拆分组合复盘与市场研究，报告归档和步骤回执继续共享 Spring 事务，子模型在事务外运行，见 [R5a 记录](r5a-implementation.md)。R5 后续处理紫微 I/O、记忆与发布配置。

通用机制有合适组件就直接使用，新的业务端口只出现在真实边界。架构整改的目标是减少需要长期维护的机制和耦合，而不是增加一组带新名字的包装类。
