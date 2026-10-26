package com.financeclaw.p3;

import static org.assertj.core.api.Assertions.*;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.financeclaw.app.execution.RecoverTaskService;
import com.financeclaw.bootstrap.FinanceClawApplication;
import com.financeclaw.domain.Task.Claim;
import com.financeclaw.domain.Task.Command;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.fixture.TaskFixture;
import com.financeclaw.infrastructure.agentscope.execution.TaskAgentRuntime;
import com.financeclaw.infrastructure.task.TaskMapper;
import com.financeclaw.support.PayloadDigest;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;
import java.util.function.Supplier;
import javax.sql.DataSource;
import org.apache.ibatis.executor.Executor;
import org.apache.ibatis.mapping.MappedStatement;
import org.apache.ibatis.plugin.Interceptor;
import org.apache.ibatis.plugin.Intercepts;
import org.apache.ibatis.plugin.Invocation;
import org.apache.ibatis.plugin.Signature;
import org.apache.ibatis.session.ResultHandler;
import org.apache.ibatis.session.RowBounds;
import org.apache.ibatis.session.SqlSessionFactory;
import org.junit.jupiter.api.*;
import org.junit.jupiter.api.parallel.Execution;
import org.junit.jupiter.api.parallel.ExecutionMode;
import org.springframework.beans.factory.config.BeanPostProcessor;
import org.springframework.boot.WebApplicationType;
import org.springframework.boot.builder.SpringApplicationBuilder;
import org.springframework.boot.test.context.TestConfiguration;
import org.springframework.context.ConfigurableApplicationContext;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Import;
import org.springframework.jdbc.core.JdbcTemplate;
import org.testcontainers.containers.PostgreSQLContainer;

/**
 * 密集恢复的真实 PG 并发验收：真实受理、领取、原生 offline SDK 和 sealed receipt 沿用生产入口。 锁后屏障只暂停已经由真实 SQL
 * 锁住的行，不替代数据源、仓库、授权或执行结果；背景 Worker 均关闭。 所有 owner/ID/正文只用于内存断言，报告仅含安全计数、固定布尔和 SQL 模板摘要。
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@Execution(ExecutionMode.SAME_THREAD)
class CommandRecoveryBatchIT {
    private static final Owner ALICE = new Owner("recovery-batch", "alice");
    private static final Owner BOB = new Owner("recovery-batch", "bob");
    private static final String CLAIM_STATEMENT =
            TaskMapper.class.getName() + ".getNextExpiredCommandForUpdateSkipLocked";
    private static final String CLAIM_HASH =
            "a95a516135cdf5aec51687f9e4cde68c27bea7f05767a49c082d5a89dc00ab67";
    private final PostgreSQLContainer<?> postgres =
            new PostgreSQLContainer<>("postgres:16.10-alpine");
    private final List<Map<String, Object>> measurements = new CopyOnWriteArrayList<>();
    private final RecoveryGate gate = new RecoveryGate();
    private ConfigurableApplicationContext application;
    private DataSource original;
    private JdbcTemplate raw;
    private JdbcExecutionCounter counter;
    private TaskFixture tasks;
    private TaskAgentRuntime runtime;
    private RecoverTaskService recovery;

    record Observed<T>(
            T value,
            Throwable failure,
            JdbcExecutionCounter.Measurement sql,
            Map<String, Object> evidence) {}

    record Item(
            List<Map<String, Object>> commands,
            List<Map<String, Object>> turns,
            List<Map<String, Object>> attempts,
            List<Map<String, Object>> events,
            List<Map<String, Object>> journal,
            List<Map<String, Object>> states) {}

    static final class RawSource {
        private DataSource source;
    }

    @TestConfiguration(proxyBeanMethods = false)
    @Import(TaskFixture.class)
    static class CountingConfiguration {
        @Bean
        static JdbcExecutionCounter recoveryBatchCounter() {
            return new JdbcExecutionCounter();
        }

        @Bean
        static RawSource recoveryBatchRawSource() {
            return new RawSource();
        }

        @Bean
        static BeanPostProcessor recoveryBatchSource(JdbcExecutionCounter counter, RawSource raw) {
            return new BeanPostProcessor() {
                @Override
                public Object postProcessAfterInitialization(Object bean, String name) {
                    if (name.equals("dataSource") && bean instanceof DataSource source) {
                        raw.source = source;
                        return counter.wrap(source);
                    }
                    return bean;
                }
            };
        }
    }

    /** 仅在生产 SQL 已返回真实已锁 Command 之后暂停；不输出 ID，不改谓词或结果。 */
    @Intercepts(
            @Signature(
                    type = Executor.class,
                    method = "query",
                    args = {
                        MappedStatement.class,
                        Object.class,
                        RowBounds.class,
                        ResultHandler.class
                    }))
    static final class RecoveryGate implements Interceptor {
        record Barrier(
                CountDownLatch arrived,
                CountDownLatch release,
                AtomicInteger slots,
                Set<String> commands) {}

        private final AtomicReference<Barrier> active = new AtomicReference<>();

        Barrier arm(int arrivals) {
            var barrier =
                    new Barrier(
                            new CountDownLatch(arrivals),
                            new CountDownLatch(1),
                            new AtomicInteger(arrivals),
                            ConcurrentHashMap.newKeySet());
            if (!active.compareAndSet(null, barrier))
                throw new IllegalStateException("Recovery gate already active");
            return barrier;
        }

        void clear() {
            var barrier = active.getAndSet(null);
            if (barrier != null) barrier.release().countDown();
        }

        @Override
        public Object intercept(Invocation invocation) throws Throwable {
            Object result = invocation.proceed();
            var barrier = active.get();
            if (barrier != null
                    && ((MappedStatement) invocation.getArgs()[0]).getId().equals(CLAIM_STATEMENT)
                    && result instanceof List<?> rows
                    && !rows.isEmpty()
                    && rows.getFirst() instanceof Command command
                    && barrier.slots().getAndDecrement() > 0) {
                barrier.commands().add(command.id());
                barrier.arrived().countDown();
                if (!barrier.release().await(8, TimeUnit.SECONDS))
                    throw new AssertionError("Recovery gate was not released");
            }
            return result;
        }
    }

    @BeforeAll
    void start() {
        try {
            postgres.start();
            application =
                    new SpringApplicationBuilder(
                                    FinanceClawApplication.class, CountingConfiguration.class)
                            .web(WebApplicationType.SERVLET)
                            .run(
                                    "--spring.datasource.url=" + postgres.getJdbcUrl(),
                                    "--spring.datasource.username=" + postgres.getUsername(),
                                    "--spring.datasource.password=" + postgres.getPassword(),
                                    "--spring.datasource.hikari.maximum-pool-size=8",
                                    "--spring.config.import=",
                                    "--server.port=0",
                                    "--spring.main.banner-mode=off",
                                    "--financeclaw.worker.enabled=false",
                                    "--financeclaw.integrations.enabled=false",
                                    "--financeclaw.memory.enabled=false",
                                    "--financeclaw.memory.profile-enabled=false",
                                    "--financeclaw.memory.profile-directory=",
                                    "--financeclaw.memory.forget-ledger.enabled=false",
                                    "--financeclaw.feishu.enabled=false",
                                    "--financeclaw.business.ziwei.enabled=false",
                                    "--financeclaw.auth.token=synthetic-recovery-batch-token",
                                    "--financeclaw.model.name=offline",
                                    "--financeclaw.fallback.name=");
            counter = application.getBean(JdbcExecutionCounter.class);
            original = application.getBean(RawSource.class).source;
            raw = new JdbcTemplate(original);
            tasks = application.getBean(TaskFixture.class);
            runtime = application.getBean(TaskAgentRuntime.class);
            recovery = application.getBean(RecoverTaskService.class);
            var configuration = application.getBean(SqlSessionFactory.class).getConfiguration();
            String sql =
                    configuration.getMappedStatement(CLAIM_STATEMENT).getBoundSql(null).getSql();
            assertThat(
                            PayloadDigest.sha256(
                                    sql.replaceAll("\\s+", " ")
                                            .trim()
                                            .toLowerCase(java.util.Locale.ROOT)))
                    .isEqualTo(CLAIM_HASH);
            configuration.addInterceptor(gate);
        } catch (RuntimeException | Error failure) {
            closeResources();
            throw failure;
        }
    }

    @BeforeEach
    void reset() {
        gate.clear();
        dropFault();
        raw.execute(
                "TRUNCATE agent_runtime.states,business.conversations,business.admission,memory.owners,memory.jobs,memory.operations,memory.audit CASCADE");
    }

    @AfterEach
    void clearGateAndFault() {
        gate.clear();
        dropFault();
    }

    @AfterAll
    void writeAndClose() throws Exception {
        try {
            var report = new LinkedHashMap<String, Object>();
            report.put("schema", "command-recovery-batch/1");
            report.put("phase", "P3a.20");
            report.put(
                    "scope",
                    "real-pg-owned-spring-concurrent-production-recovery-native-offline-runtime");
            report.put("background_pollers_enabled", false);
            report.put(
                    "measurement_boundary",
                    "production-recoverExpired-only-fixture-locks-oracles-and-gates-excluded");
            report.put("claim_template_sha256", CLAIM_HASH);
            report.put("max_claim_transactions_per_call", 50);
            report.put(
                    "no_latency_capacity_global_drain_or_unconditional_sql_reduction_claim", true);
            report.put("measurements", measurements);
            Path output = Path.of("target/p3a20/command-recovery-batch.json");
            Files.createDirectories(output.getParent());
            new ObjectMapper().writerWithDefaultPrettyPrinter().writeValue(output.toFile(), report);
        } finally {
            closeResources();
        }
    }

    @Test
    void skipsFiftyLockedExpiredCommandsAndRecoversTheNextOne() throws Exception {
        var samples = claims(51, false);
        var lockedBefore = samples.subList(0, 50).stream().map(this::item).toList();
        try (var held = hold(samples.subList(0, 50));
                var workers = Executors.newSingleThreadExecutor()) {
            try {
                var observed =
                        workers.submit(
                                        () ->
                                                observe(
                                                        "fifty-locked-prefix-next-one",
                                                        recovery::recoverExpired))
                                .get(5, TimeUnit.SECONDS);
                assertThat(observed.failure()).isNull();
                assertThat(observed.value()).isEqualTo(1);
                assertClaims(observed, 2, 2, 0);
                assertThat(samples.subList(0, 50).stream().map(this::item).toList())
                        .isEqualTo(lockedBefore);
                assertBlocked(samples.getLast());
                observed.evidence()
                        .put(
                                "business_facts",
                                Map.of(
                                        "locked_prefix",
                                        50,
                                        "later_item_recovered",
                                        1,
                                        "all_locked_items_unchanged",
                                        true,
                                        "unknown_later_item_blocked",
                                        true,
                                        "no_attempt_or_sdk_replay",
                                        true));
            } finally {
                // 即使回归为等待锁而触发超时，也先释放夹具锁，再关闭 executor。
                held.rollback();
            }
        }
    }

    @Test
    void concurrentRecoverersLockDifferentExpiredCommandsWithoutDuplicatingCompletions()
            throws Exception {
        var samples = claims(2, true);
        var barrier = gate.arm(2);
        try (var workers = Executors.newFixedThreadPool(2)) {
            var one =
                    workers.submit(() -> observe("different-items-one", recovery::recoverExpired));
            var two =
                    workers.submit(() -> observe("different-items-two", recovery::recoverExpired));
            try {
                assertThat(barrier.arrived().await(5, TimeUnit.SECONDS)).isTrue();
                assertThat(barrier.commands()).hasSize(2);
            } finally {
                barrier.release().countDown();
            }
            for (var observed :
                    List.of(one.get(8, TimeUnit.SECONDS), two.get(8, TimeUnit.SECONDS))) {
                assertThat(observed.failure()).isNull();
                assertThat(observed.value()).isEqualTo(1);
                assertClaims(observed, 2, 2, 0);
                observed.evidence()
                        .put(
                                "business_facts",
                                Map.of(
                                        "two_distinct_command_locks_observed",
                                        true,
                                        "recovered_by_this_call",
                                        1,
                                        "total_completed",
                                        2,
                                        "assistant_per_turn",
                                        1,
                                        "attempt_per_turn",
                                        1));
            }
            samples.forEach(this::assertCompletedOnce);
        }
    }

    @Test
    void aLockedRecoveryIsSkippedByItsConcurrentPeerAndCompletesOnce() throws Exception {
        var sample = claims(1, true).getFirst();
        var barrier = gate.arm(1);
        try (var workers = Executors.newFixedThreadPool(2)) {
            var one =
                    workers.submit(() -> observe("same-item-lock-owner", recovery::recoverExpired));
            Observed<Integer> skipped;
            try {
                assertThat(barrier.arrived().await(5, TimeUnit.SECONDS)).isTrue();
                skipped =
                        workers.submit(
                                        () ->
                                                observe(
                                                        "same-item-skipping-peer",
                                                        recovery::recoverExpired))
                                .get(5, TimeUnit.SECONDS);
                assertThat(skipped.failure()).isNull();
                assertThat(skipped.value()).isZero();
                assertClaims(skipped, 1, 1, 0);
                assertThat(skipped.sql().count("turn_read")).isZero();
                assertThat(skipped.sql().count("receipt_read")).isZero();
            } finally {
                barrier.release().countDown();
            }
            var completed = one.get(8, TimeUnit.SECONDS);
            assertThat(completed.failure()).isNull();
            assertThat(completed.value()).isEqualTo(1);
            assertClaims(completed, 2, 2, 0);
            assertCompletedOnce(sample);
            for (var observed : List.of(completed, skipped))
                observed.evidence()
                        .put(
                                "business_facts",
                                Map.of(
                                        "recovered_by_this_call",
                                        observed.value(),
                                        "one_completion",
                                        true,
                                        "one_assistant",
                                        true,
                                        "one_attempt",
                                        true,
                                        "skipping_peer_no_receipt_read",
                                        true));
        }
    }

    @Test
    void eachCallProcessesAtMostFiftyAndSubsequentCallDrainsTheRemainder() {
        var samples = claims(53, false);
        var first = observe("bounded-first-fifty", recovery::recoverExpired);
        assertThat(first.failure()).isNull();
        assertThat(first.value()).isEqualTo(50);
        assertClaims(first, 50, 50, 0);
        samples.subList(0, 50).forEach(this::assertBlocked);
        samples.subList(50, 53).forEach(c -> assertThat(status(c)).isEqualTo("running"));
        first.evidence()
                .put(
                        "business_facts",
                        Map.of(
                                "recovered",
                                50,
                                "remaining_running",
                                3,
                                "no_51st_claim",
                                true,
                                "unknowns_blocked_without_replay",
                                true));
        var second = observe("bounded-next-three", recovery::recoverExpired);
        assertThat(second.failure()).isNull();
        assertThat(second.value()).isEqualTo(3);
        assertClaims(second, 4, 4, 0);
        samples.forEach(this::assertBlocked);
        second.evidence()
                .put(
                        "business_facts",
                        Map.of(
                                "recovered",
                                3,
                                "remaining_running",
                                0,
                                "terminal_empty_probe",
                                true,
                                "unknowns_blocked_without_replay",
                                true));
        var empty = observe("bounded-empty", recovery::recoverExpired);
        assertThat(empty.failure()).isNull();
        assertThat(empty.value()).isZero();
        assertClaims(empty, 1, 1, 0);
        empty.evidence()
                .put(
                        "business_facts",
                        Map.of(
                                "recovered",
                                0,
                                "remaining_running",
                                0,
                                "empty_is_not_global_drain_proof",
                                true));
    }

    @Test
    void secondItemFailureRollsBackOnlyItsTransactionAndKeepsTheFirstCommit() {
        var first = claim(ALICE, true, 1);
        var second = claim(BOB, true, 2);
        var third = claim(ALICE, false, 3);
        var secondBefore = item(second);
        var thirdBefore = item(third);
        raw.execute(
                "CREATE FUNCTION business.recovery_batch_fail() RETURNS trigger LANGUAGE plpgsql AS $$ "
                        + "BEGIN IF NEW.role='assistant' AND EXISTS(SELECT 1 FROM business.commands c WHERE c.turn_id=NEW.turn_id "
                        + "AND c.lease_until=TIMESTAMPTZ '2000-01-01 00:00:02+00') THEN "
                        + "RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='synthetic recovery second-item fault'; END IF; RETURN NEW; END $$");
        raw.execute(
                "CREATE TRIGGER recovery_batch_fail AFTER INSERT ON business.journal FOR EACH ROW EXECUTE FUNCTION business.recovery_batch_fail()");
        var failed = observe("second-item-fault", recovery::recoverExpired);
        assertThat(failed.failure())
                .isNotNull()
                .hasStackTraceContaining("synthetic recovery second-item fault");
        assertClaims(failed, 2, 1, 1);
        assertCompletedOnce(first);
        assertThat(item(second)).isEqualTo(secondBefore);
        assertThat(item(third)).isEqualTo(thirdBefore);
        failed.evidence()
                .put(
                        "business_facts",
                        Map.of(
                                "first_committed_once",
                                true,
                                "second_full_snapshot_rolled_back",
                                true,
                                "third_not_attempted",
                                true,
                                "failed_call_did_not_return_a_recovered_count",
                                true));
        dropFault();
        var retry = observe("second-item-explicit-next-call-retry", recovery::recoverExpired);
        assertThat(retry.failure()).isNull();
        assertThat(retry.value()).isEqualTo(2);
        assertClaims(retry, 3, 3, 0);
        assertCompletedOnce(first);
        assertCompletedOnce(second);
        assertBlocked(third);
        retry.evidence()
                .put(
                        "business_facts",
                        Map.of(
                                "recovered",
                                2,
                                "first_assistant_still_one",
                                true,
                                "second_completed_once",
                                true,
                                "third_unknown_blocked",
                                true,
                                "retry_is_an_explicit_separate_call",
                                true));
    }

    private List<Claim> claims(int count, boolean sealed) {
        var result = new ArrayList<Claim>();
        for (int i = 1; i <= count; i++) result.add(claim(i % 2 == 0 ? BOB : ALICE, sealed, i));
        return result;
    }

    private Claim claim(Owner owner, boolean sealed, int order) {
        String conversation = tasks.createConversation(owner);
        var accepted =
                tasks.submit(
                        owner,
                        conversation,
                        UUID.randomUUID().toString(),
                        "Dense recovery fixture.");
        var claimed = tasks.claim("recovery-batch-worker", 60000);
        assertThat(claimed).isNotNull();
        assertThat(claimed.turn().id()).isEqualTo(accepted.turnId());
        if (sealed) {
            var outcome = runtime.execute(claimed);
            assertThat(outcome.status()).isEqualTo("completed");
            assertThat(outcome.stateVersion()).isPositive();
            tasks.seal(claimed, outcome);
        }
        assertThat(
                        raw.update(
                                "UPDATE business.commands SET lease_until=TIMESTAMPTZ '2000-01-01 00:00:00+00'+?*interval '1 second' WHERE id=? AND attempt_id=?",
                                order,
                                claimed.command().id(),
                                claimed.command().attemptId()))
                .isOne();
        return claimed;
    }

    private Connection hold(List<Claim> items) throws Exception {
        var connection = original.getConnection();
        connection.setAutoCommit(false);
        try (var statement =
                connection.prepareStatement(
                        "SELECT id FROM business.commands WHERE id=ANY(?) ORDER BY lease_until FOR UPDATE")) {
            var ids =
                    connection.createArrayOf(
                            "text",
                            items.stream().map(c -> c.command().id()).toArray(String[]::new));
            try {
                statement.setArray(1, ids);
                try (var rows = statement.executeQuery()) {
                    int locked = 0;
                    while (rows.next()) locked++;
                    assertThat(locked).isEqualTo(items.size());
                }
            } finally {
                ids.free();
            }
            return connection;
        } catch (Exception failure) {
            connection.close();
            throw failure;
        }
    }

    private String status(Claim claim) {
        return raw.queryForObject(
                "SELECT status FROM business.turns WHERE id=?", String.class, claim.turn().id());
    }

    private void assertBlocked(Claim sample) {
        var turn =
                tasks.get(
                        new Owner(sample.turn().tenant(), sample.turn().subject()),
                        sample.turn().conversationId(),
                        sample.turn().id());
        assertThat(turn.status()).isEqualTo("blocked");
        assertThat(turn.reason()).isEqualTo("unconfirmed_execution");
        assertThat(turn.result()).isNull();
        assertThat(
                        raw.queryForObject(
                                "SELECT count(*)::int FROM business.attempts WHERE command_id=?",
                                Integer.class,
                                sample.command().id()))
                .isOne();
        assertThat(
                        raw.queryForObject(
                                "SELECT fence FROM business.commands WHERE id=?",
                                Long.class,
                                sample.command().id()))
                .isEqualTo(sample.command().fence() + 1);
        assertThat(
                        raw.queryForObject(
                                "SELECT count(*)::int FROM business.journal WHERE turn_id=? AND role='assistant'",
                                Integer.class,
                                sample.turn().id()))
                .isZero();
        assertThat(
                        raw.queryForObject(
                                "SELECT model_calls FROM business.turn_policies WHERE turn_id=?",
                                Integer.class,
                                sample.turn().id()))
                .isZero();
    }

    private void assertCompletedOnce(Claim sample) {
        assertThat(status(sample)).isEqualTo("completed");
        assertThat(
                        raw.queryForObject(
                                "SELECT count(*)::int FROM business.journal WHERE turn_id=? AND role='assistant'",
                                Integer.class,
                                sample.turn().id()))
                .isOne();
        assertThat(
                        raw.queryForObject(
                                "SELECT count(*)::int FROM business.attempts WHERE command_id=?",
                                Integer.class,
                                sample.command().id()))
                .isOne();
        assertThat(
                        raw.queryForObject(
                                "SELECT fence FROM business.commands WHERE id=?",
                                Long.class,
                                sample.command().id()))
                .isEqualTo(sample.command().fence() + 1);
    }

    private Item item(Claim c) {
        var scope = c.scope();
        return new Item(
                raw.queryForList("SELECT * FROM business.commands WHERE id=?", c.command().id()),
                raw.queryForList("SELECT * FROM business.turns WHERE id=?", c.turn().id()),
                raw.queryForList(
                        "SELECT * FROM business.attempts WHERE command_id=? ORDER BY id",
                        c.command().id()),
                raw.queryForList(
                        "SELECT * FROM business.events WHERE turn_id=? ORDER BY revision",
                        c.turn().id()),
                raw.queryForList(
                        "SELECT * FROM business.journal WHERE turn_id=? ORDER BY sequence",
                        c.turn().id()),
                raw.queryForList(
                        "SELECT * FROM agent_runtime.states WHERE namespace=? AND user_id=? AND session_id=? ORDER BY state_key",
                        scope.namespace(),
                        scope.userId(),
                        scope.sessionId()));
    }

    private <T> Observed<T> observe(String phase, Supplier<T> work) {
        counter.begin(phase);
        T result = null;
        Throwable failure = null;
        try {
            result = work.get();
        } catch (Throwable caught) {
            failure = caught;
        }
        var sql = counter.finish();
        var evidence = new LinkedHashMap<String, Object>();
        evidence.put("phase", phase);
        evidence.put("completed_without_exception", failure == null);
        evidence.put("sql", sql);
        if (result instanceof Integer recovered) evidence.put("recovered", recovered);
        if (failure != null) evidence.put("failure_type", failure.getClass().getSimpleName());
        evidence.put("business_facts", Map.of());
        measurements.add(evidence);
        return new Observed<>(result, failure, sql, evidence);
    }

    private static void assertClaims(
            Observed<?> observed, long claims, int commits, int rollbacks) {
        assertThat(observed.sql().count("command_recovery_claim")).isEqualTo(claims);
        assertThat(observed.sql().count("command_expired_poll")).isZero();
        assertThat(observed.sql().count("command_lock")).isZero();
        assertThat(observed.sql().commits()).isEqualTo(commits);
        assertThat(observed.sql().rollbacks()).isEqualTo(rollbacks);
        assertThat(
                        observed.sql().executions().stream()
                                .filter(e -> e.family().equals("command_recovery_claim"))
                                .allMatch(e -> e.templateSha256().equals(CLAIM_HASH)))
                .isTrue();
        assertThat(
                        observed.sql().executions().stream()
                                .map(JdbcExecutionCounter.SqlExecution::connection)
                                .distinct())
                .hasSize(commits + rollbacks);
    }

    private void dropFault() {
        if (raw != null)
            raw.execute("DROP FUNCTION IF EXISTS business.recovery_batch_fail() CASCADE");
    }

    private void closeResources() {
        gate.clear();
        try {
            if (application != null) application.close();
        } finally {
            postgres.stop();
        }
    }
}
