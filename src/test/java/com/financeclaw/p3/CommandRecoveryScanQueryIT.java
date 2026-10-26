package com.financeclaw.p3;

import static org.assertj.core.api.Assertions.*;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.financeclaw.app.execution.RecoverTaskService;
import com.financeclaw.bootstrap.FinanceClawApplication;
import com.financeclaw.domain.Task.Claim;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.fixture.TaskFixture;
import com.financeclaw.infrastructure.agentscope.execution.TaskAgentRuntime;
import com.financeclaw.infrastructure.task.TaskMapper;
import com.financeclaw.support.PayloadDigest;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.sql.Timestamp;
import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;
import java.util.concurrent.locks.LockSupport;
import java.util.function.Supplier;
import javax.sql.DataSource;
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
import org.springframework.jdbc.datasource.DataSourceUtils;
import org.springframework.transaction.support.TransactionTemplate;
import org.testcontainers.containers.PostgreSQLContainer;

/**
 * 过期候选发现的真实 PG 基线。正常样本通过生产受理、领取及 offline 运行创建，观察边界只包含生产 mapper 或 RecoverTaskService；原 command/Turn
 * 锁后 live、sealed receipt、心跳和未知执行恢复均不改写。
 *
 * <p>20,000 条 future running 副本明确属于隔离规模实验，每行有独立 conversation/Turn，保持 FK/unique。 合成行不交给恢复器或
 * SDK；LIMIT 50 的 51 候选负载与少量真实 expired 的扫描成本实验分别标注。
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@Execution(ExecutionMode.SAME_THREAD)
class CommandRecoveryScanQueryIT {
    private static final Owner ALICE = new Owner("command-scan-test", "alice");
    private static final Owner BOB = new Owner("command-scan-test", "bob");
    private static final int SYNTHETIC_ROWS = 20000;
    private static final String OLD_SQL =
            "SELECT id FROM business.commands WHERE status='running' AND "
                    + "lease_until<=clock_timestamp() ORDER BY lease_until LIMIT 50";
    private static final String NEW_SQL =
            "SELECT id FROM business.commands WHERE status='running' AND "
                    + "lease_until<=(SELECT clock_timestamp()) ORDER BY lease_until LIMIT 50";
    private static final String OLD_HASH =
            "ca0387bb296498bc409bccd18444b1a9f5e566d5599d366f969584dfe76370ca";
    private static final String NEW_HASH =
            "7708dbca971c83e1dac1dfb3ecf045e77ad3eae0c65f7a36b54599994bc6807b";
    private static final List<String> PHASES =
            List.of(
                    "matrix-first",
                    "matrix-repeat",
                    "post-candidate-renew-skip",
                    "post-candidate-complete-skip",
                    "unconfirmed-recovery",
                    "unconfirmed-repeat",
                    "transaction-live-before-wait",
                    "transaction-expired-current-clock",
                    "scale-sparse-mapper",
                    "scale-limit-mapper");

    private final ObjectMapper json = new ObjectMapper();
    private final PostgreSQLContainer<?> postgres =
            new PostgreSQLContainer<>("postgres:16.10-alpine");
    private final Map<String, Map<String, Object>> measurements = new ConcurrentHashMap<>();
    private final List<Map<String, Object>> scaleExperiments = new ArrayList<>();
    private ConfigurableApplicationContext application;
    private DataSource original;
    private JdbcTemplate raw;
    private JdbcExecutionCounter counter;
    private TaskFixture tasks;
    private TaskMapper db;
    private TaskAgentRuntime runtime;
    private RecoverTaskService recovery;
    private boolean baseline;
    private String scanHash;
    private String scanSql;

    record Observed<T>(
            T value,
            Throwable failure,
            JdbcExecutionCounter.Measurement sql,
            Map<String, Object> evidence) {}

    record Snapshot(
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
        static JdbcExecutionCounter commandScanCounter() {
            return new JdbcExecutionCounter();
        }

        @Bean
        static RawSource commandScanRawSource() {
            return new RawSource();
        }

        @Bean
        static BeanPostProcessor commandScanSource(JdbcExecutionCounter counter, RawSource raw) {
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

    @BeforeAll
    void start() {
        try {
            baseline = Boolean.getBoolean("financeclaw.test.command-recovery-scan-baseline");
            if (Boolean.getBoolean("financeclaw.test.query-baseline"))
                throw new IllegalArgumentException("Use the P3a.19 dedicated baseline flag");
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
                                    "--financeclaw.auth.token=synthetic-command-scan-token",
                                    "--financeclaw.model.name=offline",
                                    "--financeclaw.fallback.name=");
            counter = application.getBean(JdbcExecutionCounter.class);
            original = application.getBean(RawSource.class).source;
            raw = new JdbcTemplate(original);
            tasks = application.getBean(TaskFixture.class);
            db = application.getBean(TaskMapper.class);
            runtime = application.getBean(TaskAgentRuntime.class);
            recovery = application.getBean(RecoverTaskService.class);
            scanSql =
                    application
                            .getBean(SqlSessionFactory.class)
                            .getConfiguration()
                            .getMappedStatement(
                                    TaskMapper.class.getName() + ".listExpiredCommandIds")
                            .getBoundSql(null)
                            .getSql();
            scanHash = digest(scanSql);
            assertThat(digest(OLD_SQL)).isEqualTo(OLD_HASH);
            assertThat(digest(NEW_SQL)).isEqualTo(NEW_HASH);
            assertThat(scanHash).isEqualTo(baseline ? OLD_HASH : NEW_HASH);
            assertThat(
                            raw.queryForObject(
                                    "SELECT pg_get_indexdef('business.command_expiry'::regclass)",
                                    String.class))
                    .contains("(lease_until)", "status = 'running'");
        } catch (RuntimeException | Error failure) {
            closeResources();
            throw failure;
        }
    }

    @BeforeEach
    void reset() {
        raw.execute(
                "TRUNCATE agent_runtime.states,business.conversations,business.admission,"
                        + "memory.owners,memory.jobs,memory.operations,memory.audit CASCADE");
    }

    @AfterAll
    void writeAndClose() throws Exception {
        try {
            var ordered = new ArrayList<Map<String, Object>>();
            for (String phase : PHASES)
                if (measurements.containsKey(phase)) ordered.add(measurements.get(phase));
            var report = new LinkedHashMap<String, Object>();
            report.put("schema", "command-recovery-scan-sql/1");
            report.put("phase", "P3a.19");
            report.put("mode", baseline ? "before" : "after");
            report.put(
                    "scope", "real-pg-owned-spring-production-mapper-recovery-and-offline-runtime");
            report.put("background_pollers_enabled", false);
            report.put(
                    "measurement_boundary",
                    "only-production-expired-mapper-or-recoverExpired-fixture-lock-clock-and-explain-excluded");
            report.put(
                    "cutoff_semantics",
                    "before-per-row-db-clock-after-initplan-db-clock-neither-transaction-nor-exact-statement-start");
            report.put(
                    "synthetic_scope",
                    "isolated-unique-conversation-turn-command-clones-never-recovered-or-executed");
            report.put("no_capacity_ttl_or_sql_count_improvement_claim", true);
            report.put("scan_template_sha256", scanHash);
            report.put("measurements", ordered);
            report.put("scale_experiments", scaleExperiments);
            Path output = Path.of("target/p3a19/command-recovery-scan-sql.json");
            Files.createDirectories(output.getParent());
            json.writerWithDefaultPrettyPrinter().writeValue(output.toFile(), report);
        } finally {
            closeResources();
        }
    }

    @Test
    void realOwnerStatusAndLeaseMatrixPreservesOrderAndDoesNotMutateTheCandidates() {
        var completed = claim(ALICE);
        executeAndSeal(completed);
        tasks.finish(completed);
        var expiredAlice = claim(ALICE);
        var expiredBob = claim(BOB);
        var expiredTail = claim(ALICE);
        var future = claim(BOB);
        var missing = claim(ALICE);
        String queuedConversation = tasks.createConversation(BOB);
        tasks.submit(
                BOB, queuedConversation, UUID.randomUUID().toString(), "Queued matrix fixture.");
        lease(expiredAlice, "TIMESTAMPTZ '2000-01-01 00:00:01+00'");
        lease(expiredBob, "TIMESTAMPTZ '2000-01-01 00:00:02+00'");
        lease(expiredTail, "TIMESTAMPTZ '2000-01-01 00:00:03+00'");
        lease(future, "TIMESTAMPTZ '2099-01-01 00:00:00+00'");
        lease(missing, "NULL");
        var expected =
                List.of(
                        expiredAlice.command().id(),
                        expiredBob.command().id(),
                        expiredTail.command().id());
        var before = snapshot();
        for (String phase : List.of("matrix-first", "matrix-repeat")) {
            var observed = observe(phase, db::listExpiredCommandIds);
            boolean unchanged = snapshot().equals(before);
            observed.evidence()
                    .put(
                            "business_facts",
                            Map.of(
                                    "candidate_count",
                                    observed.value().size(),
                                    "expected_count",
                                    3,
                                    "owner_and_status_exclusions_match",
                                    observed.value().equals(expected),
                                    "lease_order_matches",
                                    observed.value().equals(expected),
                                    "full_snapshot_unchanged",
                                    unchanged,
                                    "real_commands",
                                    7));
            assertScanOnly(observed);
            assertThat(observed.value()).containsExactlyElementsOf(expected);
            assertThat(unchanged).isTrue();
        }
    }

    @Test
    void renewedOrFinishedCommandsAreSkippedAfterCandidateDiscoveryWaitsForTheirRowLock()
            throws Exception {
        for (String change : List.of("renew", "complete")) {
            if (change.equals("complete")) reset();
            var sample = claim(ALICE);
            if (change.equals("complete")) executeAndSeal(sample);
            Instant oldDeadline = setShortLease(sample);
            var executor = Executors.newSingleThreadExecutor();
            var running = new AtomicReference<java.util.concurrent.Future<Observed<Integer>>>();
            var expected = new AtomicReference<Snapshot>();
            try {
                application
                        .getBean(TransactionTemplate.class)
                        .executeWithoutResult(
                                tx -> {
                                    assertThat(db.getCommandForUpdate(sample.command().id()).live())
                                            .isTrue();
                                    // 续租/完成发生在旧 lease 仍有效时，持有未提交结果；不复活已经过期的执行。
                                    if (change.equals("renew"))
                                        assertThat(tasks.heartbeat(sample, 60000)).isFalse();
                                    else tasks.finish(sample);
                                    var source = application.getBean(DataSource.class);
                                    Connection connection = DataSourceUtils.getConnection(source);
                                    try {
                                        int backend = backend(connection);
                                        expected.set(snapshot(connection));
                                        awaitDatabaseClock(oldDeadline);
                                        running.set(
                                                executor.submit(
                                                        () ->
                                                                observe(
                                                                        "post-candidate-"
                                                                                + change
                                                                                + "-skip",
                                                                        recovery::recoverExpired)));
                                        awaitBlockedBy(backend);
                                    } catch (java.sql.SQLException failure) {
                                        throw new IllegalStateException(failure);
                                    } finally {
                                        DataSourceUtils.releaseConnection(connection, source);
                                    }
                                });
                var observed = running.get().get(8, TimeUnit.SECONDS);
                boolean unchanged = snapshot().equals(expected.get());
                observed.evidence()
                        .put(
                                "business_facts",
                                Map.of(
                                        "candidate_observed_before_lock",
                                        true,
                                        "actual_pg_lock_wait_observed",
                                        true,
                                        "legitimate_live_lease_change_before_expiry",
                                        true,
                                        "recovered_count",
                                        observed.value(),
                                        "locked_new_version_preserved",
                                        unchanged));
                assertOneScan(observed);
                assertThat(observed.value()).isZero();
                assertThat(observed.sql().statements()).isEqualTo(2);
                assertThat(observed.sql().count("command_lock")).isOne();
                assertThat(observed.sql().count("turn_read")).isZero();
                assertThat(observed.sql().commits()).isOne();
                assertThat(observed.sql().rollbacks()).isZero();
                assertThat(unchanged).isTrue();
            } finally {
                executor.shutdownNow();
                assertThat(executor.awaitTermination(8, TimeUnit.SECONDS)).isTrue();
            }
        }
    }

    @Test
    void anExecutedButUnsealedAttemptIsBlockedWithoutRepeatingSdkOrModelWork() {
        var sample = claim(BOB);
        var outcome = runtime.execute(sample);
        assertThat(outcome.status()).isEqualTo("completed");
        assertThat(outcome.stateVersion()).isPositive();
        var before = snapshot();
        Map<String, Object> budget = policy(sample);
        assertThat(
                        raw.queryForObject(
                                "SELECT outcome IS NULL FROM business.attempts WHERE id=?",
                                Boolean.class,
                                sample.command().attemptId()))
                .isTrue();
        lease(sample, "TIMESTAMPTZ '2000-01-01 00:00:00+00'");
        var observed = observe("unconfirmed-recovery", recovery::recoverExpired);
        var turn = tasks.get(BOB, sample.turn().conversationId(), sample.turn().id());
        var after = snapshot();
        boolean sdkPreserved = after.states().equals(before.states());
        boolean budgetsPreserved = policy(sample).equals(budget);
        observed.evidence()
                .put(
                        "business_facts",
                        Map.of(
                                "recovered_count",
                                observed.value(),
                                "blocked_unconfirmed",
                                turn.status().equals("blocked")
                                        && "unconfirmed_execution".equals(turn.reason()),
                                "sdk_state_unchanged",
                                sdkPreserved,
                                "model_tool_budget_unchanged",
                                budgetsPreserved,
                                "attempt_count",
                                integer(
                                        "SELECT count(*)::int FROM business.attempts WHERE command_id=?",
                                        sample.command().id()),
                                "assistant_count",
                                integer(
                                        "SELECT count(*)::int FROM business.journal WHERE turn_id=? AND role='assistant'",
                                        sample.turn().id())));
        assertOneScan(observed);
        assertThat(observed.value()).isEqualTo(1);
        assertThat(turn.status()).isEqualTo("blocked");
        assertThat(turn.reason()).isEqualTo("unconfirmed_execution");
        assertThat(turn.result()).isNull();
        assertThat(sdkPreserved).isTrue();
        assertThat(budgetsPreserved).isTrue();
        assertThat(after.attempts()).isEqualTo(before.attempts());
        assertThat(after.journal()).isEqualTo(before.journal());
        assertThat(
                        raw.queryForObject(
                                "SELECT status FROM business.commands WHERE id=?",
                                String.class,
                                sample.command().id()))
                .isEqualTo("done");
        var repeated = observe("unconfirmed-repeat", recovery::recoverExpired);
        boolean stable = snapshot().equals(after);
        repeated.evidence()
                .put(
                        "business_facts",
                        Map.of(
                                "recovered_count",
                                repeated.value(),
                                "full_snapshot_unchanged",
                                stable));
        assertScanOnly(repeated);
        assertThat(repeated.value()).isZero();
        assertThat(stable).isTrue();
    }

    @Test
    void aLongTransactionUsesCurrentDatabaseClockRatherThanItsStartTimestamp() {
        var sample = claim(ALICE);
        lease(sample, "TIMESTAMPTZ '2099-01-01 00:00:00+00'");
        var live = observe("transaction-live-before-wait", db::listExpiredCommandIds);
        live.evidence()
                .put(
                        "business_facts",
                        Map.of(
                                "candidate_count",
                                live.value().size(),
                                "future_lease_excluded",
                                live.value().isEmpty()));
        assertScanOnly(live);
        assertThat(live.value()).isEmpty();
        application
                .getBean(TransactionTemplate.class)
                .executeWithoutResult(
                        tx -> {
                            var source = application.getBean(DataSource.class);
                            Connection connection = DataSourceUtils.getConnection(source);
                            try {
                                // 显式建立本事务起点；之后只有一次无参数生产 mapper 查询，避免一级缓存伪造结果。
                                try (var statement = connection.createStatement();
                                        var rows =
                                                statement.executeQuery(
                                                        "SELECT transaction_timestamp()")) {
                                    assertThat(rows.next()).isTrue();
                                }
                                Instant deadline;
                                try (var statement =
                                        connection.prepareStatement(
                                                "UPDATE business.commands SET lease_until=clock_timestamp()+interval '600 milliseconds' WHERE id=? RETURNING lease_until")) {
                                    statement.setString(1, sample.command().id());
                                    try (var rows = statement.executeQuery()) {
                                        assertThat(rows.next()).isTrue();
                                        deadline = rows.getTimestamp(1).toInstant();
                                    }
                                }
                                awaitDatabaseClock(deadline);
                                try (var statement =
                                        connection.prepareStatement(
                                                "SELECT transaction_timestamp()<? AND clock_timestamp()>=?")) {
                                    statement.setTimestamp(1, Timestamp.from(deadline));
                                    statement.setTimestamp(2, Timestamp.from(deadline));
                                    try (var rows = statement.executeQuery()) {
                                        assertThat(rows.next()).isTrue();
                                        assertThat(rows.getBoolean(1)).isTrue();
                                    }
                                }
                                var before = snapshot(connection);
                                var expired =
                                        observe(
                                                "transaction-expired-current-clock",
                                                db::listExpiredCommandIds);
                                boolean unchanged = snapshot(connection).equals(before);
                                expired.evidence()
                                        .put(
                                                "business_facts",
                                                Map.of(
                                                        "candidate_count",
                                                        expired.value().size(),
                                                        "transaction_start_precedes_lease",
                                                        true,
                                                        "database_clock_reached_lease",
                                                        true,
                                                        "full_snapshot_unchanged",
                                                        unchanged));
                                assertScanOnly(expired);
                                assertThat(expired.value()).containsExactly(sample.command().id());
                                assertThat(unchanged).isTrue();
                            } catch (java.sql.SQLException failure) {
                                throw new IllegalStateException(failure);
                            } finally {
                                DataSourceUtils.releaseConnection(connection, source);
                            }
                        });
    }

    @Test
    void defaultPlannerBoundsSparseExpiredReadsAndTheSeparateSyntheticLimitRemainsFifty()
            throws Exception {
        var first = claim(ALICE);
        var second = claim(BOB);
        var third = claim(ALICE);
        lease(first, "TIMESTAMPTZ '2000-01-01 00:00:01+00'");
        lease(second, "TIMESTAMPTZ '2000-01-01 00:00:02+00'");
        lease(third, "TIMESTAMPTZ '2000-01-01 00:00:03+00'");
        createSyntheticFutureHistory(first);
        raw.execute("ANALYZE business.commands");
        var expected = List.of(first.command().id(), second.command().id(), third.command().id());
        var before = snapshot();
        var sparse = observe("scale-sparse-mapper", db::listExpiredCommandIds);
        boolean unchanged = snapshot().equals(before);
        sparse.evidence()
                .put(
                        "business_facts",
                        Map.of(
                                "candidate_count",
                                sparse.value().size(),
                                "real_expired_candidates",
                                3,
                                "synthetic_future_rows",
                                SYNTHETIC_ROWS,
                                "lease_order_matches",
                                sparse.value().equals(expected),
                                "full_snapshot_unchanged",
                                unchanged,
                                "synthetic_not_recovered_or_executed",
                                true));
        assertScanOnly(sparse);
        assertThat(sparse.value()).containsExactlyElementsOf(expected);
        assertThat(unchanged).isTrue();
        explainSparseScale(expected);
        assertThat(snapshot()).isEqualTo(before);

        // 这是另一个纯 mapper 限额实验，不改变稀疏规模计划的负载，也不将合成命令交给恢复链路。
        assertThat(
                        raw.update(
                                "UPDATE business.commands SET lease_until=TIMESTAMPTZ '2001-01-01 00:00:00+00' "
                                        + "+substring(id from length('synthetic-scan-command-')+1)::int*interval '1 second' "
                                        + "WHERE id IN (SELECT 'synthetic-scan-command-'||g FROM generate_series(1,48) g)"))
                .isEqualTo(48);
        var limitedExpected = new ArrayList<>(expected);
        for (int i = 1; i <= 47; i++) limitedExpected.add("synthetic-scan-command-" + i);
        var beforeLimit = snapshot();
        var limited = observe("scale-limit-mapper", db::listExpiredCommandIds);
        boolean limitUnchanged = snapshot().equals(beforeLimit);
        limited.evidence()
                .put(
                        "business_facts",
                        Map.of(
                                "candidate_count",
                                limited.value().size(),
                                "eligible_candidates",
                                51,
                                "limit",
                                50,
                                "real_expired_candidates",
                                3,
                                "synthetic_expired_candidates",
                                48,
                                "lease_order_matches",
                                limited.value().equals(limitedExpected),
                                "full_snapshot_unchanged",
                                limitUnchanged,
                                "synthetic_not_recovered_or_executed",
                                true));
        assertScanOnly(limited);
        assertThat(limited.value()).containsExactlyElementsOf(limitedExpected).hasSize(50);
        assertThat(limitUnchanged).isTrue();
    }

    private Claim claim(Owner owner) {
        String conversation = tasks.createConversation(owner);
        var accepted =
                tasks.submit(
                        owner,
                        conversation,
                        UUID.randomUUID().toString(),
                        "Command recovery scan fixture.");
        var claim = tasks.claim("command-scan-worker", 60000);
        assertThat(claim).isNotNull();
        assertThat(claim.turn().id()).isEqualTo(accepted.turnId());
        return claim;
    }

    private void executeAndSeal(Claim claim) {
        var outcome = runtime.execute(claim);
        assertThat(outcome.status()).isEqualTo("completed");
        assertThat(outcome.stateVersion()).isPositive();
        tasks.seal(claim, outcome);
    }

    private void lease(Claim claim, String expression) {
        if (!expression.matches("TIMESTAMPTZ '[0-9:+ -]+'|NULL"))
            throw new IllegalArgumentException("Unknown isolated lease fixture");
        assertThat(
                        raw.update(
                                "UPDATE business.commands SET lease_until="
                                        + expression
                                        + " WHERE id=? AND attempt_id=? AND status='running'",
                                claim.command().id(),
                                claim.command().attemptId()))
                .isOne();
    }

    private Instant setShortLease(Claim sample) {
        return raw.queryForObject(
                "UPDATE business.commands SET lease_until=clock_timestamp()+interval '4 seconds' "
                        + "WHERE id=? AND attempt_id=? AND status='running' RETURNING lease_until",
                (rs, n) -> rs.getTimestamp(1).toInstant(),
                sample.command().id(),
                sample.command().attemptId());
    }

    private Map<String, Object> policy(Claim sample) {
        return raw.queryForMap(
                "SELECT model_calls,tool_calls FROM business.turn_policies WHERE turn_id=?",
                sample.turn().id());
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
        var sql = counter.finish();
        var row = new LinkedHashMap<String, Object>();
        row.put("phase", phase);
        row.put("completed_without_exception", failure == null);
        row.put("sql", sql);
        row.put("business_facts", Map.of());
        if (failure != null) row.put("failure_type", failure.getClass().getSimpleName());
        if (measurements.putIfAbsent(phase, row) != null)
            throw new IllegalStateException("Duplicate measurement phase");
        return new Observed<>(value, failure, sql, row);
    }

    private void assertOneScan(Observed<?> measured) {
        assertThat(measured.failure()).isNull();
        assertThat(measured.sql().count("command_expired_poll")).isOne();
        assertThat(measured.sql().executions().getFirst().templateSha256()).isEqualTo(scanHash);
    }

    private void assertScanOnly(Observed<?> measured) {
        assertOneScan(measured);
        assertThat(measured.sql().statements()).isOne();
        assertThat(measured.sql().commits()).isZero();
        assertThat(measured.sql().rollbacks()).isZero();
    }

    private void createSyntheticFutureHistory(Claim seed) {
        assertThat(
                        raw.update(
                                "INSERT INTO business.conversations(id,tenant,subject) "
                                        + "SELECT 'synthetic-scan-conversation-'||g,tenant,subject FROM business.conversations "
                                        + "CROSS JOIN generate_series(1,20000) g WHERE id=?",
                                seed.turn().conversationId()))
                .isEqualTo(SYNTHETIC_ROWS);
        assertThat(
                        raw.update(
                                "INSERT INTO business.turns(id,conversation_id,tenant,subject,idempotency_key,request_hash,message,release_hash,status,agent_id) "
                                        + "SELECT 'synthetic-scan-turn-'||g,'synthetic-scan-conversation-'||g,tenant,subject,'synthetic-key-'||g,request_hash,message,release_hash,'running',agent_id "
                                        + "FROM business.turns CROSS JOIN generate_series(1,20000) g WHERE id=?",
                                seed.turn().id()))
                .isEqualTo(SYNTHETIC_ROWS);
        assertThat(
                        raw.update(
                                "INSERT INTO business.commands(id,turn_id,status,fence,lease_until,worker_id) "
                                        + "SELECT 'synthetic-scan-command-'||g,'synthetic-scan-turn-'||g,'running',0,"
                                        + "TIMESTAMPTZ '2099-01-01 00:00:00+00'+g*interval '1 second','synthetic-plan-only' "
                                        + "FROM generate_series(1,20000) g"))
                .isEqualTo(SYNTHETIC_ROWS);
        assertThat(
                        integer(
                                "SELECT count(*)::int FROM business.commands WHERE id LIKE 'synthetic-scan-command-%' AND attempt_id IS NULL"))
                .isEqualTo(SYNTHETIC_ROWS);
    }

    private void explainSparseScale(List<String> expected) throws Exception {
        String encoded =
                raw.queryForObject(
                        "EXPLAIN (ANALYZE,BUFFERS,VERBOSE,FORMAT JSON) " + scanSql, String.class);
        var plan = json.readTree(encoded).get(0).path("Plan");
        assertThat(plan.path("Actual Rows").asInt()).isEqualTo(expected.size());
        var nodes = new ArrayList<JsonNode>();
        collectNodes(plan, nodes);
        var safe = new ArrayList<Map<String, Object>>();
        for (var node : nodes) {
            String cond = node.path("Index Cond").asText();
            var row = new LinkedHashMap<String, Object>();
            row.put("node_type", node.path("Node Type").asText());
            row.put("relation_name", node.path("Relation Name").asText(""));
            row.put("index_name", node.path("Index Name").asText(""));
            row.put("actual_rows", node.path("Actual Rows").asDouble());
            row.put("actual_loops", node.path("Actual Loops").asDouble());
            row.put("rows_removed_by_filter", node.path("Rows Removed by Filter").asDouble());
            row.put("shared_hit_blocks", node.path("Shared Hit Blocks").asLong());
            row.put("shared_read_blocks", node.path("Shared Read Blocks").asLong());
            row.put("shared_dirtied_blocks", node.path("Shared Dirtied Blocks").asLong());
            row.put("shared_written_blocks", node.path("Shared Written Blocks").asLong());
            row.put("heap_fetches", node.path("Heap Fetches").asLong());
            row.put(
                    "parent_relationship_is_initplan",
                    node.path("Parent Relationship").asText().equals("InitPlan"));
            row.put(
                    "output_contains_clock",
                    node.path("Output").toString().contains("clock_timestamp"));
            row.put("index_condition_contains_parameter", cond.contains("$"));
            row.put("index_condition_contains_lease_until", cond.contains("lease_until"));
            row.put("index_condition_contains_clock_timestamp", cond.contains("clock_timestamp"));
            row.put(
                    "filter_contains_volatile_clock",
                    node.path("Filter").asText().contains("clock_timestamp"));
            safe.add(row);
        }
        var row = new LinkedHashMap<String, Object>();
        row.put("phase", baseline ? "scale-before-production" : "scale-after-production");
        row.put("production_sql", true);
        row.put("default_planner", true);
        row.put("template_sha256", scanHash);
        row.put("synthetic_future_running_rows", SYNTHETIC_ROWS);
        row.put("real_expired_rows", 3);
        row.put("actual_result_rows", expected.size());
        row.put("scan_is_read_only", true);
        row.put("synthetic_not_recovered_or_executed", true);
        row.put("safe_plan_nodes", safe);
        row.put(
                "plan_export",
                "safe-projection-only-no-output-or-condition-text-root-buffers-include-children");
        scaleExperiments.add(row);
        assertThat(nodes).anyMatch(n -> n.path("Index Name").asText().equals("command_expiry"));
        if (!baseline) {
            var clocks =
                    nodes.stream()
                            .filter(
                                    n ->
                                            n.path("Parent Relationship")
                                                            .asText()
                                                            .equals("InitPlan")
                                                    && n.path("Node Type").asText().equals("Result")
                                                    && n.path("Output")
                                                            .toString()
                                                            .contains("clock_timestamp"))
                            .toList();
            assertThat(clocks).hasSize(1);
            assertThat(clocks.getFirst().path("Actual Rows").asDouble()).isEqualTo(1.0);
            assertThat(clocks.getFirst().path("Actual Loops").asDouble()).isEqualTo(1.0);
            assertThat(nodes)
                    .anyMatch(
                            n ->
                                    n.path("Index Name").asText().equals("command_expiry")
                                            && n.path("Index Cond").asText().contains("lease_until")
                                            && n.path("Index Cond").asText().contains("$"));
        }
    }

    private Snapshot snapshot() {
        return new Snapshot(
                raw.queryForList("SELECT * FROM business.commands ORDER BY id"),
                raw.queryForList("SELECT * FROM business.turns ORDER BY id"),
                raw.queryForList("SELECT * FROM business.attempts ORDER BY id"),
                raw.queryForList("SELECT * FROM business.events ORDER BY turn_id,revision"),
                raw.queryForList("SELECT * FROM business.journal ORDER BY sequence"),
                raw.queryForList(
                        "SELECT * FROM agent_runtime.states ORDER BY namespace,user_id,session_id,state_key"));
    }

    private Snapshot snapshot(Connection connection) throws java.sql.SQLException {
        return new Snapshot(
                rows(connection, "SELECT * FROM business.commands ORDER BY id"),
                rows(connection, "SELECT * FROM business.turns ORDER BY id"),
                rows(connection, "SELECT * FROM business.attempts ORDER BY id"),
                rows(connection, "SELECT * FROM business.events ORDER BY turn_id,revision"),
                rows(connection, "SELECT * FROM business.journal ORDER BY sequence"),
                rows(
                        connection,
                        "SELECT * FROM agent_runtime.states ORDER BY namespace,user_id,session_id,state_key"));
    }

    private static List<Map<String, Object>> rows(Connection connection, String sql)
            throws java.sql.SQLException {
        var result = new ArrayList<Map<String, Object>>();
        try (var statement = connection.createStatement();
                var rows = statement.executeQuery(sql)) {
            var metadata = rows.getMetaData();
            while (rows.next()) {
                var row = new LinkedHashMap<String, Object>();
                for (int column = 1; column <= metadata.getColumnCount(); column++)
                    row.put(metadata.getColumnLabel(column), rows.getObject(column));
                result.add(row);
            }
        }
        return result;
    }

    private void awaitDatabaseClock(Instant deadline) {
        long bounded = System.nanoTime() + Duration.ofSeconds(5).toNanos();
        do {
            if (Boolean.TRUE.equals(
                    raw.queryForObject(
                            "SELECT clock_timestamp()>=?",
                            Boolean.class,
                            Timestamp.from(deadline)))) return;
            LockSupport.parkNanos(TimeUnit.MILLISECONDS.toNanos(5));
        } while (System.nanoTime() < bounded);
        throw new AssertionError("Database clock did not reach the isolated lease deadline");
    }

    private void awaitBlockedBy(int holder) {
        long deadline = System.nanoTime() + Duration.ofSeconds(5).toNanos();
        do {
            if (integer(
                            "SELECT count(*)::int FROM pg_stat_activity a WHERE a.datname=current_database() "
                                    + "AND a.pid<>pg_backend_pid() AND ?=ANY(pg_blocking_pids(a.pid))",
                            holder)
                    > 0) return;
            LockSupport.parkNanos(TimeUnit.MILLISECONDS.toNanos(5));
        } while (System.nanoTime() < deadline);
        throw new AssertionError("Expired command recovery did not reach the held command lock");
    }

    private static int backend(Connection connection) throws java.sql.SQLException {
        try (var statement = connection.createStatement();
                var result = statement.executeQuery("SELECT pg_backend_pid()")) {
            assertThat(result.next()).isTrue();
            return result.getInt(1);
        }
    }

    private int integer(String sql, Object... parameters) {
        return raw.queryForObject(sql, Integer.class, parameters);
    }

    private static String digest(String sql) {
        return PayloadDigest.sha256(sql.replaceAll("\\s+", " ").trim().toLowerCase(Locale.ROOT));
    }

    private static void collectNodes(JsonNode node, List<JsonNode> nodes) {
        nodes.add(node);
        for (var child : node.path("Plans")) collectNodes(child, nodes);
    }

    private void closeResources() {
        try {
            if (application != null) application.close();
        } finally {
            postgres.stop();
        }
    }
}
