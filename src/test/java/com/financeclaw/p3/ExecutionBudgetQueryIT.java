package com.financeclaw.p3;

import static org.assertj.core.api.Assertions.*;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.financeclaw.app.agent.AgentService;
import com.financeclaw.app.execution.ChildInvocationService;
import com.financeclaw.app.execution.ExecutionPolicyService;
import com.financeclaw.app.governance.GrantService;
import com.financeclaw.app.market.MarketDataService;
import com.financeclaw.app.memory.MemoryService;
import com.financeclaw.app.release.ReleaseService;
import com.financeclaw.app.task.SubmitTaskService;
import com.financeclaw.bootstrap.FinanceClawApplication;
import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.Task.*;
import com.financeclaw.domain.agent.AgentComposition;
import com.financeclaw.domain.agent.PrivateAgentTarget;
import com.financeclaw.domain.release.BusinessFeatures;
import com.financeclaw.domain.release.ReleaseDefinition;
import com.financeclaw.infrastructure.agentscope.skills.PackagedSkills;
import com.financeclaw.support.PayloadJson;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicReference;
import java.util.function.Supplier;
import javax.sql.DataSource;
import org.junit.jupiter.api.*;
import org.junit.jupiter.api.parallel.Execution;
import org.junit.jupiter.api.parallel.ExecutionMode;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.springframework.beans.factory.config.BeanPostProcessor;
import org.springframework.boot.WebApplicationType;
import org.springframework.boot.builder.SpringApplicationBuilder;
import org.springframework.boot.test.context.TestConfiguration;
import org.springframework.context.ConfigurableApplicationContext;
import org.springframework.context.annotation.Bean;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.transaction.UnexpectedRollbackException;
import org.testcontainers.containers.PostgreSQLContainer;

/**
 * 在真实 PG/JDBC execute 边界比较本地执行预算的 SQL 成本。正常发布、受理、领取及 begin 均在观察窗口之外； 不 mock 生产端口、不运行模型或后台
 * Worker。P3a.7 去掉 consume 内第二次发布解析，P3a.9 将每次解析合并为一次主/基础清单查询； 外层子调用和内层行情读取的全部
 * fence、owner、source、policy、grant 门禁继续独立执行。
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@Execution(ExecutionMode.SAME_THREAD)
class ExecutionBudgetQueryIT {
    private static final Owner ALICE = new Owner("query-budget-test", "alice");
    private static final Owner BOB = new Owner("query-budget-test", "bob");
    private static final String CALL = "counted-private-call";
    private static final String TOOL_CALL = "counted-snapshot";
    private static final Map<String, Object> MESSAGE =
            Map.of("message", "Explicit market request.");
    private static final Map<String, Object> SNAPSHOT = Map.of("symbol", "AAPL");
    private static final List<String> GATES =
            List.of(
                    "command_lock",
                    "turn_read",
                    "owner_lock",
                    "source_read",
                    "policy_read",
                    "grant_lock");
    private final PostgreSQLContainer<?> postgres =
            new PostgreSQLContainer<>("postgres:16.10-alpine");
    private final List<JdbcExecutionCounter.Measurement> measurements =
            new CopyOnWriteArrayList<>();
    private ConfigurableApplicationContext application;
    private JdbcExecutionCounter counter;
    private JdbcTemplate sql;
    private AgentService agents;
    private ReleaseService releases;
    private SubmitTaskService submission;
    private com.financeclaw.app.execution.ExecutionLeaseService leases;
    private ExecutionPolicyService policies;
    private ChildInvocationService children;
    private MarketDataService market;
    private GrantService grants;
    private MemoryService memory;

    record Scenario(
            Owner owner,
            Claim claim,
            PrivateAgentTarget target,
            ChildInvocationService.PrivateInvocation started) {}

    record Observed<T>(T value, Throwable failure, JdbcExecutionCounter.Measurement sql) {}

    /** 只包装应用主 dataSource；事务管理器和 MyBatis 会取得相同的 wrapper，不能各用一套资源身份。 */
    @TestConfiguration(proxyBeanMethods = false)
    static class CountingConfiguration {
        @Bean
        static JdbcExecutionCounter executionSqlCounter() {
            return new JdbcExecutionCounter();
        }

        @Bean
        static BeanPostProcessor countingDataSource(JdbcExecutionCounter counter) {
            return new BeanPostProcessor() {
                @Override
                public Object postProcessAfterInitialization(Object bean, String name) {
                    return name.equals("dataSource") && bean instanceof DataSource source
                            ? counter.wrap(source)
                            : bean;
                }
            };
        }
    }

    @BeforeAll
    void start() {
        postgres.start();
        try {
            application =
                    new SpringApplicationBuilder(
                                    FinanceClawApplication.class, CountingConfiguration.class)
                            .web(WebApplicationType.SERVLET)
                            .run(
                                    "--spring.datasource.url=" + postgres.getJdbcUrl(),
                                    "--spring.datasource.username=" + postgres.getUsername(),
                                    "--spring.datasource.password=" + postgres.getPassword(),
                                    "--spring.datasource.hikari.maximum-pool-size=6",
                                    "--spring.config.import=",
                                    "--server.port=0",
                                    "--spring.main.banner-mode=off",
                                    "--financeclaw.worker.enabled=false",
                                    "--financeclaw.integrations.enabled=false",
                                    "--financeclaw.memory.enabled=false",
                                    "--financeclaw.memory.profile-enabled=true",
                                    "--financeclaw.memory.profile-directory=",
                                    "--financeclaw.memory.forget-ledger.enabled=false",
                                    "--financeclaw.feishu.enabled=false",
                                    "--financeclaw.business.ziwei.enabled=false",
                                    "--financeclaw.model.name=offline",
                                    "--financeclaw.fallback.name=");
            counter = application.getBean(JdbcExecutionCounter.class);
            sql = new JdbcTemplate(application.getBean(DataSource.class));
            agents = application.getBean(AgentService.class);
            releases = application.getBean(ReleaseService.class);
            submission = application.getBean(SubmitTaskService.class);
            leases = application.getBean(com.financeclaw.app.execution.ExecutionLeaseService.class);
            policies = application.getBean(ExecutionPolicyService.class);
            children = application.getBean(ChildInvocationService.class);
            market = application.getBean(MarketDataService.class);
            grants = application.getBean(GrantService.class);
            memory = application.getBean(MemoryService.class);
        } catch (RuntimeException | Error failure) {
            if (application != null) application.close();
            postgres.stop();
            throw failure;
        }
    }

    @BeforeEach
    void reset() {
        sql.execute(
                "TRUNCATE agent_runtime.states,business.conversations,memory.owners,memory.jobs,memory.operations,memory.audit CASCADE");
        sql.update(
                "UPDATE memory.privacy_ledger_binding SET lineage=?,active=false",
                UUID.randomUUID().toString());
    }

    @AfterAll
    void writeMeasurementsAndClose() throws Exception {
        try {
            // 各 observe 在性能断言之前已经完成记录；即使旧生产版本性能断言为红，也保留完整基线。
            Path destination = Path.of("target/p3a7/sql-counts.json");
            Files.createDirectories(destination.getParent());
            new ObjectMapper()
                    .writerWithDefaultPrettyPrinter()
                    .writeValue(
                            destination.toFile(),
                            Map.of(
                                    "format", "execution-budget-sql/1",
                                    "measurement_boundary",
                                            "JDBC execute methods; fixture preparation and verification excluded",
                                    "connection_id_semantics",
                                            "unique JDBC connection borrow; nested REQUIRED shares the same id",
                                    "expected_change",
                                            "one joined root/base SQL per release resolution; P3a.7 consume reuse and every authority gate retained",
                                    "measurements", List.copyOf(measurements)));
        } finally {
            // 资源归本测试所有，避免 PG 已停止而缓存 Spring/Hikari 上下文继续重连。
            try {
                if (application != null) application.close();
            } finally {
                postgres.stop();
            }
        }
    }

    @Test
    void firstPrivateSnapshotAndReplayUseJoinedReleaseReadsAndKeepEveryGate() {
        var scenario = scenario(ALICE, 32, 64);
        var first = observe("private-first", () -> snapshot(scenario, TOOL_CALL));
        var replay =
                observe(
                        "private-replay",
                        () ->
                                children.privateSnapshot(
                                        scenario.started().invocation(),
                                        TOOL_CALL,
                                        Map.of("symbol", "aapl"),
                                        () -> {
                                            throw new AssertionError(
                                                    "Cached snapshot cannot invoke its supplier");
                                        }));
        // 先完成两份观察与数据验证，再断性能，旧生产基线不会因首次断言变红而漏掉回放样本。
        assertThat(first.failure()).isNull();
        assertThat(replay.failure()).isNull();
        assertThat(replay.value()).isEqualTo(first.value());
        assertThat(((Map<?, ?>) first.value()).get("price")).isEqualTo("123.45");
        assertThat(((Map<?, ?>) first.value()).get("source_release_hash"))
                .isEqualTo(scenario.target().releaseHash());
        assertCounters(scenario, 0, 1, 0, 1);
        assertThat(stepCount(scenario)).isOne();
        assertGates(first.sql(), 2);
        assertGates(replay.sql(), 1);
        assertFamilies(
                first.sql(),
                Map.of(
                        "child_read",
                        1L,
                        "step_read",
                        1L,
                        "child_tool_update",
                        1L,
                        "root_tool_update",
                        1L,
                        "step_insert",
                        1L));
        assertFamilies(replay.sql(), Map.of("child_read", 1L, "step_read", 1L));
        assertThat(replay.sql().count("root_tool_update")).isZero();
        assertThat(replay.sql().count("child_tool_update")).isZero();
        assertSingleTransaction(first.sql(), false);
        assertSingleTransaction(replay.sql(), false);
        assertThat(first.sql().manifests()).isEqualTo(3);
        assertThat(first.sql().count("release_lookup")).isEqualTo(3);
        assertThat(first.sql().statements()).isEqualTo(20);
        assertThat(replay.sql().manifests()).isEqualTo(2);
        assertThat(replay.sql().count("release_lookup")).isEqualTo(2);
        assertThat(replay.sql().statements()).isEqualTo(10);
    }

    @Test
    void modelAndDirectToolReservationsKeepTheirGatesAndUseOneCheckedBudgetRelease() {
        var scenario = scenario(ALICE, 32, 64);
        var model =
                observe(
                        "child-model-reservation",
                        () -> {
                            children.reserveModel(scenario.started().invocation());
                            return null;
                        });
        var tool =
                observe(
                        "direct-root-tool-reservation",
                        () -> {
                            policies.consumeTool(scenario.claim(), "artifact:read");
                            return null;
                        });
        assertThat(model.failure()).isNull();
        assertThat(tool.failure()).isNull();
        assertCounters(scenario, 1, 1, 1, 0);
        assertGates(model.sql(), 1);
        assertGates(tool.sql(), 1);
        assertFamilies(
                model.sql(),
                Map.of("child_read", 1L, "child_model_update", 1L, "root_model_update", 1L));
        assertFamilies(tool.sql(), Map.of("root_tool_update", 1L));
        assertSingleTransaction(model.sql(), false);
        assertSingleTransaction(tool.sql(), false);
        assertThat(model.sql().manifests()).isEqualTo(2);
        assertThat(model.sql().statements()).isEqualTo(11);
        assertThat(tool.sql().manifests()).isOne();
        assertThat(tool.sql().statements()).isEqualTo(8);
    }

    @Test
    void fullRootBudgetRollsBackChildReservationAndNeverPersistsTheSnapshot() {
        var scenario = scenario(ALICE, 32, 1);
        policies.consumeTool(scenario.claim(), "artifact:read");
        var failed = observe("root-tool-budget-exhausted", () -> snapshot(scenario, TOOL_CALL));
        assertBlocked(failed, "execution_budget_exhausted");
        assertCounters(scenario, 0, 1, 0, 0);
        assertThat(stepCount(scenario)).isZero();
        assertGates(failed.sql(), 2);
        assertFamilies(
                failed.sql(),
                Map.of(
                        "child_read",
                        1L,
                        "step_read",
                        1L,
                        "child_tool_update",
                        1L,
                        "root_tool_update",
                        1L));
        assertThat(failed.sql().count("step_insert")).isZero();
        assertSingleTransaction(failed.sql(), true);
        assertThat(failed.sql().manifests()).isEqualTo(3);
        assertThat(failed.sql().statements()).isEqualTo(19);
    }

    @ParameterizedTest
    @ValueSource(strings = {"grant", "forget", "lease-and-fence"})
    void aLaterTransactionCannotReuseAnEarlierCheckedAuthorityEvenForAStoredReceipt(String change) {
        var scenario = scenario(ALICE, 32, 64);
        assertThat(snapshot(scenario, TOOL_CALL)).isNotNull();
        var supplierInvoked = new AtomicBoolean();
        if (change.equals("lease-and-fence")) {
            var old = scenario.claim().command();
            var stale =
                    new Claim(
                            new Command(
                                    old.id(),
                                    old.turnId(),
                                    old.status(),
                                    old.attemptId(),
                                    old.fence() - 1,
                                    old.live(),
                                    old.resumeId()),
                            scenario.claim().turn());
            var fenced =
                    observe(
                            "different-fence-rejects-replay",
                            () -> cachedSnapshot(scenario, stale, supplierInvoked));
            assertThat(fenced.failure()).isInstanceOf(Fenced.class);
            assertRejectedBeforeReceipt(fenced.sql());
            assertThat(fenced.sql().statements()).isOne();
            assertNestedExpiredLeaseIsRechecked(scenario);
            sql.update(
                    "UPDATE business.commands SET lease_until=clock_timestamp()-interval '1 second' WHERE id=?",
                    old.id());
        } else if (change.equals("grant")) {
            grants.changeGrant(
                    ALICE,
                    scenario.claim().turn().conversationId(),
                    "revoke-query-test",
                    1,
                    List.of());
        } else {
            memory.forget(ALICE, 1, "forget-query-test");
        }
        var rejected =
                observe(
                        change + "-rejects-replay",
                        () -> cachedSnapshot(scenario, scenario.claim(), supplierInvoked));
        if (change.equals("grant")) assertBlocked(rejected, "authorization_changed");
        else if (change.equals("forget")) assertBlocked(rejected, "memory_privacy_changed");
        else assertThat(rejected.failure()).isInstanceOf(Fenced.class);
        assertThat(supplierInvoked).isFalse();
        assertRejectedBeforeReceipt(rejected.sql());
        assertThat(rejected.sql().count("command_lock")).isOne();
        assertThat(
                        integer(
                                "SELECT tool_calls FROM business.turn_policies WHERE turn_id=?",
                                scenario.claim().turn().id()))
                .isOne();
        assertThat(stepCount(scenario)).isEqualTo(change.equals("forget") ? 0 : 1);
        if (change.equals("lease-and-fence")) {
            assertThat(rejected.sql().statements()).isOne();
        } else {
            assertThat(rejected.sql().manifests()).isOne();
            assertThat(rejected.sql().statements()).isEqualTo(change.equals("grant") ? 7 : 5);
            assertThat(rejected.sql().count("owner_lock")).isOne();
            assertThat(rejected.sql().count("source_read")).isOne();
            assertThat(rejected.sql().count("grant_lock"))
                    .isEqualTo(change.equals("grant") ? 1 : 0);
        }
    }

    @Test
    void sameThreadAndInterleavedOwnerThreadsKeepIndependentFrozenBudgetsAndTransactions()
            throws Exception {
        var first = scenario(ALICE, 1, 64);
        var second = scenario(BOB, 2, 64);
        // 同一线程先后消费两个不同发布，不能沿用前一个 owner 的预算投影。
        var a = observe("sequential-owner-a", () -> consumeModel(first));
        var b = observe("sequential-owner-b", () -> consumeModel(second));
        var start = new CountDownLatch(1);
        Observed<Void> aLimited;
        Observed<Void> bAllowed;
        try (var executor = Executors.newFixedThreadPool(2)) {
            var fa =
                    executor.submit(
                            () -> {
                                assertThat(start.await(10, TimeUnit.SECONDS)).isTrue();
                                return observe(
                                        "parallel-owner-a-exhausted", () -> consumeModel(first));
                            });
            var fb =
                    executor.submit(
                            () -> {
                                assertThat(start.await(10, TimeUnit.SECONDS)).isTrue();
                                return observe(
                                        "parallel-owner-b-second", () -> consumeModel(second));
                            });
            start.countDown();
            aLimited = fa.get(15, TimeUnit.SECONDS);
            bAllowed = fb.get(15, TimeUnit.SECONDS);
        }
        assertThat(a.failure()).isNull();
        assertThat(b.failure()).isNull();
        assertBlocked(aLimited, "execution_budget_exhausted");
        assertThat(bAllowed.failure()).isNull();
        assertCounters(first, 1, 0, 0, 0);
        assertCounters(second, 2, 0, 0, 0);
        assertThat(aLimited.sql().executions().getFirst().thread())
                .isNotEqualTo(bAllowed.sql().executions().getFirst().thread());
        for (var sample : List.of(a, b, aLimited, bAllowed)) {
            assertGates(sample.sql(), 1);
            assertThat(sample.sql().count("root_model_update")).isOne();
            assertSingleTransaction(sample.sql(), sample == aLimited);
            assertThat(sample.sql().manifests()).isOne();
            assertThat(sample.sql().statements()).isEqualTo(8);
        }
    }

    /** 在同一事务已经锁住 command 后缩短 lease，嵌套 consume 仍必须用数据库时钟重新判 live。 */
    private void assertNestedExpiredLeaseIsRechecked(Scenario scenario) {
        var nested = new AtomicReference<Observed<Void>>();
        assertThatThrownBy(
                        () ->
                                policies.dispatch(
                                        scenario.claim(),
                                        ignored -> {
                                            sql.update(
                                                    "UPDATE business.commands SET lease_until=clock_timestamp()-interval '1 second' WHERE id=?",
                                                    scenario.claim().command().id());
                                            nested.set(
                                                    observe(
                                                            "held-transaction-expired-lease",
                                                            () -> {
                                                                policies.consumeTool(
                                                                        scenario.claim(),
                                                                        "artifact:read");
                                                                return null;
                                                            }));
                                            return null;
                                        }))
                .isInstanceOf(UnexpectedRollbackException.class);
        assertThat(nested.get()).isNotNull();
        assertThat(nested.get().failure()).isInstanceOf(Fenced.class);
        assertRejectedBeforeReceipt(nested.get().sql());
        assertThat(nested.get().sql().statements()).isOne();
        // 嵌套参与事务的失败令外事务整体回滚，测试缩短的 lease 也不能遗留到后续阶段。
        assertThat(
                        sql.queryForObject(
                                "SELECT lease_until>clock_timestamp() FROM business.commands WHERE id=?",
                                Boolean.class,
                                scenario.claim().command().id()))
                .isTrue();
    }

    private Void consumeModel(Scenario scenario) {
        policies.consume(scenario.claim(), true);
        return null;
    }

    private Object cachedSnapshot(Scenario scenario, Claim parent, AtomicBoolean supplierInvoked) {
        var original = scenario.started().invocation();
        var invocation =
                new ChildInvocationService.Invocation(
                        parent,
                        original.callId(),
                        original.inputHash(),
                        original.cachedResult(),
                        original.snapshotResults(),
                        original.target(),
                        original.targetAgentId(),
                        original.targetReleaseHash(),
                        original.maxModelCalls(),
                        original.maxToolCalls(),
                        original.profile());
        return children.privateSnapshot(
                invocation,
                TOOL_CALL,
                SNAPSHOT,
                () -> {
                    supplierInvoked.set(true);
                    throw new AssertionError(
                            "An invalidated invocation cannot execute local reads");
                });
    }

    private Object snapshot(Scenario scenario, String call) {
        return children.privateSnapshot(
                scenario.started().invocation(),
                call,
                SNAPSHOT,
                () ->
                        market.readFrozen(
                                scenario.claim(),
                                scenario.target(),
                                scenario.started().release(),
                                "AAPL"));
    }

    private Scenario scenario(Owner owner, int rootModels, int rootTools) {
        publishBase(32, 64, "123.45");
        String leaf = UUID.randomUUID().toString();
        agents.create(
                owner,
                leaf,
                "Counted market leaf",
                "Explicit message only.",
                new AgentComposition("primary", List.of("function:market_snapshot")));
        var target =
                new PrivateAgentTarget(
                        leaf,
                        agents.publish(owner, leaf, 1, 0).releaseHash(),
                        PrivateAgentTarget.Profile.MARKET_SNAPSHOT);
        publishBase(rootModels, rootTools, "999.99");
        String parent = UUID.randomUUID().toString();
        agents.create(
                owner,
                parent,
                "Counted parent",
                "Parent context.",
                new AgentComposition("primary", List.of("agent:" + leaf)));
        agents.publish(owner, parent, 1, 0);
        String conversation = submission.createConversation(owner, parent);
        var accepted =
                submission.submit(
                        owner, conversation, UUID.randomUUID().toString(), "Count local SQL.");
        var claim = leases.claim("query-budget-test", 1200000);
        assertThat(claim.turn().id()).isEqualTo(accepted.turnId());
        return new Scenario(
                owner, claim, target, children.beginPrivate(claim, CALL, MESSAGE, target));
    }

    private void publishBase(int models, int tools, String price) {
        String snapshots =
                PayloadJson.canonical(
                        Map.of(
                                "AAPL",
                                Map.of(
                                        "price",
                                        price,
                                        "currency",
                                        "USD",
                                        "provider",
                                        "query-counter-fixture",
                                        "as_of",
                                        "2026-10-24T10:00:00Z")));
        releases.publish(
                new ReleaseDefinition(
                        "execution-budget-query-test",
                        "finance_agent",
                        ReleaseDefinition.SCOPES,
                        ReleaseDefinition.OFFLINE,
                        null,
                        models,
                        tools,
                        new BusinessFeatures(List.of(), PackagedSkills.hash(), snapshots, null)));
    }

    private <T> Observed<T> observe(String phase, Supplier<T> operation) {
        counter.begin(phase);
        T value = null;
        Throwable failure = null;
        try {
            value = operation.get();
        } catch (Throwable caught) {
            failure = caught;
        }
        var sample = counter.finish();
        measurements.add(sample);
        return new Observed<>(value, failure, sample);
    }

    private static void assertGates(JdbcExecutionCounter.Measurement measured, long expected) {
        for (String family : GATES)
            assertThat(measured.count(family))
                    .as("%s %s", measured.phase(), family)
                    .isEqualTo(expected);
        assertThat(measured.count("other"))
                .as("all physical statements have an explicit family")
                .isZero();
    }

    private static void assertFamilies(
            JdbcExecutionCounter.Measurement measured, Map<String, Long> expected) {
        expected.forEach(
                (family, count) ->
                        assertThat(measured.count(family))
                                .as("%s %s", measured.phase(), family)
                                .isEqualTo(count));
    }

    private static void assertSingleTransaction(
            JdbcExecutionCounter.Measurement measured, boolean rollback) {
        assertThat(
                        measured.executions().stream()
                                .map(JdbcExecutionCounter.SqlExecution::connection)
                                .distinct()
                                .toList())
                .hasSize(1);
        assertThat(measured.commits()).isEqualTo(rollback ? 0 : 1);
        assertThat(measured.rollbacks()).isEqualTo(rollback ? 1 : 0);
    }

    private static void assertRejectedBeforeReceipt(JdbcExecutionCounter.Measurement measured) {
        for (String family :
                List.of(
                        "child_read",
                        "step_read",
                        "step_insert",
                        "root_tool_update",
                        "root_model_update",
                        "child_tool_update",
                        "child_model_update")) {
            assertThat(measured.count(family))
                    .as("%s never reaches %s", measured.phase(), family)
                    .isZero();
        }
    }

    private static void assertBlocked(Observed<?> observed, String code) {
        assertThat(observed.failure()).isInstanceOf(Blocked.class).hasMessage(code);
    }

    private int integer(String query, Object... parameters) {
        return sql.queryForObject(query, Integer.class, parameters);
    }

    private int stepCount(Scenario scenario) {
        return integer(
                "SELECT count(*) FROM business.capability_steps WHERE turn_id=? AND namespace=?",
                scenario.claim().turn().id(),
                "child:" + CALL);
    }

    private void assertCounters(
            Scenario scenario, int rootModels, int rootTools, int childModels, int childTools) {
        String turn = scenario.claim().turn().id();
        assertThat(integer("SELECT model_calls FROM business.turn_policies WHERE turn_id=?", turn))
                .isEqualTo(rootModels);
        assertThat(integer("SELECT tool_calls FROM business.turn_policies WHERE turn_id=?", turn))
                .isEqualTo(rootTools);
        assertThat(
                        integer(
                                "SELECT model_calls FROM business.child_invocations WHERE root_turn_id=? AND call_id=?",
                                turn,
                                CALL))
                .isEqualTo(childModels);
        assertThat(
                        integer(
                                "SELECT tool_calls FROM business.child_invocations WHERE root_turn_id=? AND call_id=?",
                                turn,
                                CALL))
                .isEqualTo(childTools);
    }
}
