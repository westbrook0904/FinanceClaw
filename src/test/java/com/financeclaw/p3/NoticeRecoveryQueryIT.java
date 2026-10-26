package com.financeclaw.p3;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.financeclaw.app.execution.port.ExecutionContextGateway;
import com.financeclaw.app.task.TaskQueryService;
import com.financeclaw.app.task.WorkSignals;
import com.financeclaw.bootstrap.FinanceClawApplication;
import com.financeclaw.channel.AgentScopeFeishu;
import com.financeclaw.channel.FeishuDelivery;
import com.financeclaw.channel.FeishuService;
import com.financeclaw.channel.FeishuSettings;
import com.financeclaw.domain.Feishu.Binding;
import com.financeclaw.domain.Feishu.Notice;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.Task.Turn;
import com.financeclaw.fixture.TaskFixture;
import com.financeclaw.infrastructure.agentscope.execution.TaskAgentRuntime;
import com.financeclaw.infrastructure.channel.FeishuMapper;
import com.financeclaw.m1.TaskWorkerFixture;
import com.financeclaw.support.PayloadDigest;
import com.financeclaw.support.PayloadJson;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.time.Duration;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
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
import org.springframework.transaction.support.TransactionSynchronizationManager;
import org.springframework.transaction.support.TransactionTemplate;
import org.testcontainers.containers.PostgreSQLContainer;

/**
 * sending 失联恢复的真实 PG 基线。正常受理、offline 执行、投影、领取和结果封存使用生产服务； 只有末端飞书 send 是替身。raw
 * 只控制失联时间、隔离故障和明确标记的规模副本，不伪造正常 Turn/Attempt。
 *
 * <p>本轮仅索引优化，业务窗口前后必须执行完全相同的 SQL。JDBC 显式 commit/rollback 次数与 PG 自动提交语句的隐式事务分开解释。EXPLAIN ANALYZE
 * UPDATE 会真实执行，因此始终使用独立原始事务回滚。
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@Execution(ExecutionMode.SAME_THREAD)
class NoticeRecoveryQueryIT {
    private static final Owner ALICE = new Owner("notice-recovery-test", "alice");
    private static final Owner BOB = new Owner("notice-recovery-test", "bob");
    private static final String TENANT_KEY = "notice-recovery-tenant";
    private static final String INDEX = "channel_sending_notice";
    private static final int SYNTHETIC_ROWS = 20000;
    private static final String RECOVERY_SQL =
            "UPDATE channel.notices SET status='unknown',reason='worker_lost' WHERE"
                    + " status='sending' AND started_at<clock_timestamp()-interval '60 seconds'";
    private static final String RECOVERY_HASH =
            "3b9fffdaa266baac83b0e5be30e176e9ef16d49d65dbf2527856964cd346e2ce";
    private static final List<String> PHASES =
            List.of(
                    "status-matrix-first",
                    "status-matrix-repeat",
                    "late-success-poll",
                    "late-success-idle-first",
                    "late-success-idle-skip",
                    "late-success-recover",
                    "late-success-restart",
                    "late-failure-poll",
                    "late-failure-idle-first",
                    "late-failure-idle-skip",
                    "late-failure-recover",
                    "late-failure-restart",
                    "recheck-status-after-lock",
                    "recheck-time-after-lock",
                    "recovery-trigger-rollback",
                    "recovery-trigger-retry");

    private final ObjectMapper json = new ObjectMapper();
    private final PostgreSQLContainer<?> postgres =
            new PostgreSQLContainer<>("postgres:16.10-alpine");
    private final Map<String, Map<String, Object>> measurements = new ConcurrentHashMap<>();
    private final List<Map<String, Object>> scaleExperiments = new ArrayList<>();
    private final List<Send> sends = new CopyOnWriteArrayList<>();
    private final AtomicBoolean failSend = new AtomicBoolean();
    private final AtomicReference<Gate> sendGate = new AtomicReference<>();
    private ConfigurableApplicationContext application;
    private JdbcExecutionCounter counter;
    private DataSource original;
    private JdbcTemplate raw;
    private TaskFixture tasks;
    private FeishuMapper db;
    private FeishuService inbound;
    private FeishuSettings settings;
    private AgentScopeFeishu sender;
    private boolean baseline;

    record Send(String chat, String text) {}

    record Scenario(Owner owner, Binding binding, Turn turn) {}

    record Snapshot(Notice notice, String startedAt, String createdAt) {}

    record Observed<T>(
            T value,
            Throwable failure,
            JdbcExecutionCounter.Measurement sql,
            Map<String, Object> evidence) {}

    static final class Gate {
        final CountDownLatch entered = new CountDownLatch(1);
        final CountDownLatch release = new CountDownLatch(1);
    }

    static final class RawSource {
        private DataSource source;
    }

    @TestConfiguration(proxyBeanMethods = false)
    @Import(TaskFixture.class)
    static class CountingConfiguration {
        @Bean
        static JdbcExecutionCounter noticeRecoveryCounter() {
            return new JdbcExecutionCounter();
        }

        @Bean
        static RawSource noticeRecoveryRawSource() {
            return new RawSource();
        }

        @Bean
        static BeanPostProcessor noticeRecoverySource(JdbcExecutionCounter counter, RawSource raw) {
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
            baseline = Boolean.getBoolean("financeclaw.test.notice-recovery-baseline");
            if (Boolean.getBoolean("financeclaw.test.query-baseline") || baseline)
                throw new IllegalArgumentException(
                        "Historical recovery baseline flags are not supported by the P3a.20 regression");
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
                                    "--financeclaw.model.name=offline",
                                    "--financeclaw.fallback.name=",
                                    "--financeclaw.auth.token=synthetic-notice-recovery-token",
                                    "--financeclaw.feishu.enabled=true",
                                    "--financeclaw.feishu.app-id=recovery-test",
                                    "--financeclaw.feishu.app-secret=synthetic-recovery-secret",
                                    "--financeclaw.feishu.verification-token=synthetic-recovery-token",
                                    "--financeclaw.feishu.encrypt-key=synthetic-recovery-key",
                                    "--financeclaw.feishu.tenant-key=" + TENANT_KEY,
                                    "--financeclaw.business.ziwei.enabled=false");
            counter = application.getBean(JdbcExecutionCounter.class);
            original = application.getBean(RawSource.class).source;
            raw = new JdbcTemplate(original);
            tasks = application.getBean(TaskFixture.class);
            db = application.getBean(FeishuMapper.class);
            inbound = application.getBean(FeishuService.class);
            settings = application.getBean(FeishuSettings.class);
            sender = mock(AgentScopeFeishu.class);
            String production =
                    application
                            .getBean(SqlSessionFactory.class)
                            .getConfiguration()
                            .getMappedStatement(
                                    FeishuMapper.class.getName()
                                            + ".updateExpiredSendingNoticesUnknown")
                            .getBoundSql(null)
                            .getSql();
            assertThat(digest(production)).isEqualTo(RECOVERY_HASH).isEqualTo(digest(RECOVERY_SQL));
            assertThat(indexPresent()).isEqualTo(!baseline);
        } catch (RuntimeException | Error failure) {
            closeResources();
            throw failure;
        }
    }

    @BeforeEach
    void reset() {
        dropFailureTrigger();
        raw.execute(
                "TRUNCATE agent_runtime.states,business.conversations,business.admission,"
                        + "memory.jobs,memory.owners,memory.operations,memory.audit CASCADE");
        sends.clear();
        failSend.set(false);
        sendGate.set(null);
        org.mockito.Mockito.reset(sender);
        doAnswer(
                        call -> {
                            assertThat(
                                            TransactionSynchronizationManager
                                                    .isActualTransactionActive())
                                    .isFalse();
                            sends.add(new Send(call.getArgument(0), call.getArgument(1)));
                            var gate = sendGate.get();
                            if (gate != null) {
                                gate.entered.countDown();
                                if (!gate.release.await(8, TimeUnit.SECONDS))
                                    throw new AssertionError(
                                            "Late send response was not released within eight seconds");
                            }
                            if (failSend.get())
                                throw new IllegalStateException("synthetic_transport_failure");
                            return null;
                        })
                .when(sender)
                .send(anyString(), anyString());
    }

    @AfterAll
    void stopAndWrite() throws Exception {
        try {
            var ordered = new ArrayList<Map<String, Object>>();
            for (String phase : PHASES)
                if (measurements.containsKey(phase)) ordered.add(measurements.get(phase));
            var report = new LinkedHashMap<String, Object>();
            report.put("schema", "notice-recovery-sql/2");
            report.put("phase", "P3a.20");
            report.put("mode", baseline ? "before" : "after");
            report.put("scope", "real-pg-production-lifecycle-offline-model-terminal-send-mock");
            report.put("background_pollers_enabled", false);
            report.put(
                    "sql_boundary",
                    "production-recovery-or-poll-only-fixture-lock-observation-and-explain-excluded");
            report.put(
                    "jdbc_transactions",
                    "explicit-connection-methods-only-autocommit-statement-atomicity-verified-by-oracle");
            report.put("no_sql_count_wire_ttl_or_capacity_improvement_claim", true);
            report.put("measurements", ordered);
            report.put("scale_experiments", scaleExperiments);
            Path output = Path.of("target/p3a20/notice-recovery-sql.json");
            Files.createDirectories(output.getParent());
            json.writerWithDefaultPrettyPrinter().writeValue(output.toFile(), report);
        } finally {
            closeResources();
        }
    }

    @Test
    void onlyExpiredSendingChangesAcrossOwnersAndOtherDurableStatesStaySealed() throws Exception {
        // 先通过真实 send 建立终态，再创建 sending 和 pending，避免准备阶段消费其他待发送样本。
        var submitted = scenario(BOB);
        delivery().scheduled();
        assertState(submitted, "submitted_unverified", "native_sdk_no_receipt");
        var unknown = scenario(ALICE);
        failSend.set(true);
        delivery().scheduled();
        failSend.set(false);
        assertState(unknown, "unknown", "native_send_unconfirmed");
        var suppressed = scenario(BOB);
        delivery().project(suppressed.binding().id());
        inbound.receive(message(suppressed.binding(), "/new"));
        assertThat(delivery().claim()).isNull();
        assertState(suppressed, "suppressed", "binding_changed");
        var expiredAlice = claimed(ALICE);
        var expiredBob = claimed(BOB);
        var recent = claimed(ALICE);
        var missingTime = claimed(BOB);
        var pending = scenario(ALICE);
        delivery().project(pending.binding().id());
        setStarted(expiredAlice, "clock_timestamp()-interval '61 seconds'");
        setStarted(expiredBob, "clock_timestamp()-interval '61 seconds'");
        setStarted(recent, "clock_timestamp()+interval '1 day'");
        setStarted(missingTime, "NULL");
        var before = snapshots();
        int initialSends = sends.size();
        var first =
                observe(
                        "status-matrix-first",
                        false,
                        () -> {
                            delivery().scheduledRecovery();
                            return null;
                        });
        var after = snapshots();
        assertOnlyRecovered(
                before, after, List.of(notice(expiredAlice).id(), notice(expiredBob).id()));
        first.evidence()
                .put(
                        "business_facts",
                        Map.of(
                                "recovered_rows",
                                2,
                                "unchanged_rows",
                                6,
                                "full_notice_oracle_matches",
                                true,
                                "owner_routes_preserved",
                                true,
                                "new_send_attempts",
                                sends.size() - initialSends));
        assertRecovery(first, false);
        assertThat(sends).hasSize(initialSends);
        var repeat =
                observe(
                        "status-matrix-repeat",
                        false,
                        () -> {
                            delivery().scheduledRecovery();
                            return null;
                        });
        boolean unchanged = snapshots().equals(after);
        repeat.evidence()
                .put(
                        "business_facts",
                        Map.of(
                                "recovered_rows",
                                0,
                                "full_notice_oracle_matches",
                                unchanged,
                                "new_send_attempts",
                                sends.size() - initialSends));
        assertRecovery(repeat, false);
        assertThat(unchanged).isTrue();
    }

    @Test
    void independentRecoveryRunsDuringIdleAndLateOutcomesCannotUnsealUnknown() throws Exception {
        for (String outcome : List.of("success", "failure")) {
            if (outcome.equals("failure")) reset();
            var sample = scenario(ALICE);
            var producer = delivery();
            producer.project(sample.binding().id());
            var gate = new Gate();
            sendGate.set(gate);
            failSend.set(outcome.equals("failure"));
            var executor = Executors.newSingleThreadExecutor();
            var future =
                    executor.submit(
                            () ->
                                    observe(
                                            "late-" + outcome + "-poll",
                                            false,
                                            () -> {
                                                producer.poll();
                                                return null;
                                            }));
            try {
                assertThat(gate.entered.await(8, TimeUnit.SECONDS)).isTrue();
                assertState(sample, "sending", null);
                var coordinator = delivery();
                var idle =
                        observe(
                                "late-" + outcome + "-idle-first",
                                false,
                                () -> {
                                    coordinator.scheduled();
                                    return null;
                                });
                idle.evidence()
                        .put(
                                "business_facts",
                                Map.of("sender_blocked", true, "sending_preserved", true));
                assertSql(idle, 2, false);
                var skipped =
                        observe(
                                "late-" + outcome + "-idle-skip",
                                false,
                                () -> {
                                    coordinator.scheduled();
                                    return null;
                                });
                skipped.evidence()
                        .put(
                                "business_facts",
                                Map.of("sender_blocked", true, "sending_preserved", true));
                assertSql(skipped, 0, false);
                setStarted(sample, "clock_timestamp()-interval '61 seconds'");
                var recovered =
                        observe(
                                "late-" + outcome + "-recover",
                                false,
                                () -> {
                                    coordinator.scheduledRecovery();
                                    return null;
                                });
                assertState(sample, "unknown", "worker_lost");
                recovered
                        .evidence()
                        .put(
                                "business_facts",
                                Map.of(
                                        "sender_blocked",
                                        true,
                                        "unknown_sealed",
                                        true,
                                        "recovered_rows",
                                        1));
                assertRecovery(recovered, false);
                var sealed = snapshots();
                gate.release.countDown();
                var completed = future.get(8, TimeUnit.SECONDS);
                boolean preserved = snapshots().equals(sealed);
                completed
                        .evidence()
                        .put(
                                "business_facts",
                                Map.of(
                                        "unknown_sealed",
                                        preserved,
                                        "late_result_cannot_overwrite",
                                        preserved,
                                        "send_attempts",
                                        sends.size()));
                assertSql(completed, 6, false);
                assertThat(preserved).isTrue();
                assertThat(sends)
                        .containsExactly(
                                new Send(sample.binding().chatId(), sample.turn().result()));
                var replay =
                        observe(
                                "late-" + outcome + "-restart",
                                false,
                                () -> {
                                    delivery().poll();
                                    return null;
                                });
                replay.evidence()
                        .put(
                                "business_facts",
                                Map.of(
                                        "unknown_not_requeued",
                                        snapshots().equals(sealed),
                                        "send_attempts",
                                        sends.size(),
                                        "new_send_attempts",
                                        sends.size() - 1));
                assertSql(replay, 3, false);
                assertThat(snapshots()).isEqualTo(sealed);
                assertThat(sends).hasSize(1);
            } finally {
                gate.release.countDown();
                executor.shutdownNow();
                assertThat(executor.awaitTermination(8, TimeUnit.SECONDS)).isTrue();
                sendGate.set(null);
            }
        }
    }

    @Test
    void recoveryRechecksStatusAndStartedAtAfterARealNoticeRowLockWait() throws Exception {
        for (String change : List.of("status", "time")) {
            if (change.equals("time")) reset();
            var sample = claimed(ALICE);
            setStarted(sample, "clock_timestamp()-interval '61 seconds'");
            var executor = Executors.newSingleThreadExecutor();
            var running = new AtomicReference<java.util.concurrent.Future<Observed<Object>>>();
            var expected = new AtomicReference<List<Snapshot>>();
            try {
                application
                        .getBean(TransactionTemplate.class)
                        .executeWithoutResult(
                                tx -> {
                                    // 这个锁仅由隔离测试持有；恢复 UPDATE 必须真实等待该 PG backend，而非等待 Java latch。
                                    assertThat(db.getNoticeForUpdate(notice(sample).id()))
                                            .isNotNull();
                                    var source = application.getBean(DataSource.class);
                                    Connection connection = DataSourceUtils.getConnection(source);
                                    try {
                                        int backend;
                                        try (var statement = connection.createStatement();
                                                var result =
                                                        statement.executeQuery(
                                                                "SELECT pg_backend_pid()")) {
                                            assertThat(result.next()).isTrue();
                                            backend = result.getInt(1);
                                        }
                                        running.set(
                                                executor.submit(
                                                        () ->
                                                                observe(
                                                                        "recheck-"
                                                                                + change
                                                                                + "-after-lock",
                                                                        false,
                                                                        () -> {
                                                                            delivery()
                                                                                    .scheduledRecovery();
                                                                            return null;
                                                                        })));
                                        awaitBlockedBy(backend);
                                        if (change.equals("status")) {
                                            // 与网络返回后的原生产封存使用同一方法，不用 raw 伪造新的终态转换。
                                            db.updateNoticeOutcome(
                                                    notice(sample).id(),
                                                    "submitted_unverified",
                                                    "native_sdk_no_receipt");
                                        } else {
                                            try (var statement =
                                                    connection.prepareStatement(
                                                            "UPDATE channel.notices SET started_at=clock_timestamp()+interval '1 day' WHERE id=?")) {
                                                statement.setString(1, notice(sample).id());
                                                assertThat(statement.executeUpdate()).isOne();
                                            }
                                        }
                                        expected.set(snapshots(connection));
                                    } catch (java.sql.SQLException failure) {
                                        throw new IllegalStateException(failure);
                                    } finally {
                                        DataSourceUtils.releaseConnection(connection, source);
                                    }
                                });
                var observed = running.get().get(8, TimeUnit.SECONDS);
                assertThat(snapshots()).isEqualTo(expected.get());
                var actual = notice(sample);
                if (change.equals("status"))
                    assertState(sample, "submitted_unverified", "native_sdk_no_receipt");
                else assertState(sample, "sending", null);
                assertThat(actual.payload()).isEqualTo(sample.turn().result());
                assertThat(actual.bindingId()).isEqualTo(sample.binding().id());
                observed.evidence()
                        .put(
                                "business_facts",
                                Map.of(
                                        "actual_pg_lock_wait_observed",
                                        true,
                                        "locked_new_version_preserved",
                                        true,
                                        "recovered_rows",
                                        0,
                                        "send_attempts",
                                        sends.size()));
                assertRecovery(observed, false);
                assertThat(sends).isEmpty();
            } finally {
                executor.shutdownNow();
                assertThat(executor.awaitTermination(8, TimeUnit.SECONDS)).isTrue();
            }
        }
    }

    @Test
    void aFailedRecoveryStatementRollsBackEveryRowAndTheNextRunCanRecoverAll() throws Exception {
        var samples = List.of(claimed(ALICE), claimed(BOB), claimed(ALICE));
        for (var sample : samples) setStarted(sample, "clock_timestamp()-interval '61 seconds'");
        var before = snapshots();
        // statement trigger 在整条 UPDATE 处理完全部候选后才抛错，不依赖 Seq/Index Scan 的行访问顺序。
        // 因而完整 snapshot 的相等性证明三个修改一起回滚，而不是仅证明第一行尚未完成。
        raw.execute(
                "CREATE FUNCTION channel.p3a18_recovery_failure() RETURNS trigger LANGUAGE plpgsql AS $$"
                        + " BEGIN RAISE EXCEPTION 'notice_recovery_injected_failure'; RETURN NULL; END $$");
        raw.execute(
                "CREATE TRIGGER p3a18_recovery_failure AFTER UPDATE ON channel.notices FOR EACH STATEMENT "
                        + "EXECUTE FUNCTION channel.p3a18_recovery_failure()");
        var failed =
                observe(
                        "recovery-trigger-rollback",
                        true,
                        () -> {
                            delivery().scheduledRecovery();
                            return null;
                        });
        boolean restored = snapshots().equals(before);
        failed.evidence()
                .put(
                        "business_facts",
                        Map.of(
                                "full_notice_oracle_matches",
                                restored,
                                "recovered_rows",
                                0,
                                "statement_failure_expected",
                                true,
                                "fault_after_statement",
                                true,
                                "send_attempts",
                                sends.size()));
        assertRecovery(failed, true);
        assertThat(failed.failure()).hasStackTraceContaining("notice_recovery_injected_failure");
        assertThat(restored).isTrue();
        dropFailureTrigger();
        var retried =
                observe(
                        "recovery-trigger-retry",
                        false,
                        () -> {
                            delivery().scheduledRecovery();
                            return null;
                        });
        assertOnlyRecovered(
                before, snapshots(), samples.stream().map(s -> notice(s).id()).toList());
        retried.evidence()
                .put(
                        "business_facts",
                        Map.of(
                                "full_notice_oracle_matches",
                                true,
                                "recovered_rows",
                                3,
                                "send_attempts",
                                sends.size()));
        assertRecovery(retried, false);
        assertThat(sends).isEmpty();
    }

    @Test
    void defaultPlannerAvoidsScanningSettledHistoryAndExplainUpdateAlwaysRollsBack()
            throws Exception {
        var seed = scenario(BOB);
        delivery().scheduled();
        var template = notice(seed);
        assertState(seed, "submitted_unverified", "native_sdk_no_receipt");
        assertThat(
                        raw.update(
                                "INSERT INTO channel.notices(id,turn_id,binding_id,binding_revision,revision,payload,status,reason,started_at) "
                                        + "SELECT 'synthetic-recovery-plan-'||g,turn_id,binding_id,binding_revision,1000000+g,payload,status,reason,started_at "
                                        + "FROM channel.notices CROSS JOIN generate_series(1,20000) g WHERE id=?",
                                template.id()))
                .isEqualTo(SYNTHETIC_ROWS);
        var expired = claimed(ALICE);
        var recent = claimed(BOB);
        var nullTime = claimed(ALICE);
        setStarted(expired, "clock_timestamp()-interval '61 seconds'");
        setStarted(recent, "clock_timestamp()+interval '1 day'");
        setStarted(nullTime, "NULL");
        raw.execute("ANALYZE channel.notices");
        var before = snapshots();
        assertThat(before).hasSize(SYNTHETIC_ROWS + 4);
        JsonNode plan;
        boolean rollbackAttempted = false;
        try (var connection = original.getConnection()) {
            connection.setAutoCommit(false);
            try {
                try (var statement = connection.createStatement();
                        var result =
                                statement.executeQuery(
                                        "EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) " + RECOVERY_SQL)) {
                    assertThat(result.next()).isTrue();
                    plan = json.readTree(result.getString(1)).get(0);
                }
                // UPDATE 没有 RETURNING，ModifyTable 根 Actual Rows 固定为 0，不是受影响行数。
                assertThat(plan.path("Plan").path("Actual Rows").asInt()).isZero();
                var inside = snapshots(connection);
                assertOnlyRecovered(before, inside, List.of(notice(expired).id()));
            } finally {
                connection.rollback();
                rollbackAttempted = true;
            }
        }
        boolean restored = snapshots().equals(before);
        var nodes = new ArrayList<JsonNode>();
        collectNodes(plan.path("Plan"), nodes);
        var safe = safeNodes(nodes);
        var row = new LinkedHashMap<String, Object>();
        row.put("phase", baseline ? "scale-before-production" : "scale-after-production");
        row.put("production_sql", true);
        row.put("default_planner", true);
        row.put("index_present", indexPresent());
        row.put("template_sha256", RECOVERY_HASH);
        row.put("synthetic_settled_rows", SYNTHETIC_ROWS);
        row.put("real_sending_rows", 3);
        row.put("actual_matched_rows", 1);
        row.put("modify_table_actual_rows", 0);
        row.put("explicit_rollback", rollbackAttempted);
        row.put("full_notice_oracle_restored", restored);
        row.put("safe_plan_nodes", safe);
        row.put(
                "plan_export",
                "safe-projection-only-no-condition-text-root-buffers-include-children");
        scaleExperiments.add(row);
        assertThat(restored).isTrue();
        assertThat(indexPresent()).isEqualTo(!baseline);
        assertThat(nodes).anyMatch(n -> n.path("Filter").asText().contains("clock_timestamp"));
        assertThat(nodes).noneMatch(n -> n.path("Index Cond").asText().contains("clock_timestamp"));
        if (!baseline) {
            assertThat(nodes).anyMatch(n -> n.path("Index Name").asText().equals(INDEX));
            assertThat(nodes)
                    .noneMatch(
                            n ->
                                    n.path("Relation Name").asText().equals("notices")
                                            && n.path("Node Type").asText().contains("Seq Scan"));
            String definition =
                    raw.queryForObject(
                            "SELECT pg_get_indexdef('channel.channel_sending_notice'::regclass)",
                            String.class);
            assertThat(definition).contains("(started_at)", "status = 'sending'");
        }
        assertThat(sends).containsExactly(new Send(seed.binding().chatId(), seed.turn().result()));
    }

    private FeishuDelivery delivery() {
        return new FeishuDelivery(
                db,
                application.getBean(TaskQueryService.class),
                application.getBean(ExecutionContextGateway.class),
                application.getBean(TransactionTemplate.class),
                sender,
                application.getBean(WorkSignals.class),
                settings,
                true,
                10000,
                10000);
    }

    private Scenario scenario(Owner owner) throws Exception {
        String conversation = tasks.createConversation(owner);
        String suffix = UUID.randomUUID().toString();
        var identity =
                new Binding(
                        "unused",
                        TENANT_KEY,
                        "open-" + suffix,
                        "chat-" + suffix,
                        owner.tenant(),
                        owner.subject(),
                        conversation,
                        1,
                        false,
                        null);
        inbound.receive(message(identity, "/bind " + inbound.link(owner, conversation)));
        inbound.receive(message(identity, "recovery " + UUID.randomUUID()));
        var bound =
                db.getBindingByChannelIdentityForUpdate(
                        TENANT_KEY, identity.openId(), identity.chatId());
        try (var worker =
                TaskWorkerFixture.create(
                        application,
                        application.getBean(TaskAgentRuntime.class),
                        false,
                        1,
                        60000,
                        2000)) {
            var claim = tasks.claim("notice-recovery-test", 60000);
            assertThat(claim).isNotNull();
            assertThat(claim.turn().id()).isEqualTo(bound.activeTurn());
            worker.execute(claim);
        }
        var turn = tasks.get(owner, conversation, bound.activeTurn());
        assertThat(turn.status())
                .as("normal offline fixture: %s", turn.reason())
                .isEqualTo("completed");
        return new Scenario(owner, bound, turn);
    }

    private Scenario claimed(Owner owner) throws Exception {
        var sample = scenario(owner);
        var delivery = delivery();
        delivery.project(sample.binding().id());
        var candidate = notice(sample);
        assertThat(delivery.claim()).isEqualTo(candidate);
        assertState(sample, "sending", null);
        return sample;
    }

    private JsonNode message(Binding identity, String text) {
        return json.valueToTree(
                Map.of(
                        "schema",
                        "2.0",
                        "header",
                        Map.of(
                                "event_id",
                                UUID.randomUUID().toString(),
                                "event_type",
                                "im.message.receive_v1",
                                "app_id",
                                settings.appId,
                                "tenant_key",
                                TENANT_KEY,
                                "token",
                                settings.token),
                        "event",
                        Map.of(
                                "sender",
                                Map.of(
                                        "sender_id",
                                        Map.of("open_id", identity.openId()),
                                        "sender_type",
                                        "user"),
                                "message",
                                Map.of(
                                        "chat_id",
                                        identity.chatId(),
                                        "chat_type",
                                        "p2p",
                                        "message_id",
                                        "message-" + UUID.randomUUID(),
                                        "message_type",
                                        "text",
                                        "content",
                                        PayloadJson.canonical(Map.of("text", text))))));
    }

    private void setStarted(Scenario sample, String expression) {
        if (!List.of(
                        "clock_timestamp()-interval '61 seconds'",
                        "clock_timestamp()+interval '1 day'",
                        "NULL")
                .contains(expression))
            throw new IllegalArgumentException("Unknown isolated clock fixture");
        assertThat(
                        raw.update(
                                "UPDATE channel.notices SET started_at="
                                        + expression
                                        + " WHERE id=? AND status='sending'",
                                notice(sample).id()))
                .isOne();
    }

    private <T> Observed<T> observe(String phase, boolean expectedFailure, Supplier<T> operation) {
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
        row.put("expected_failure", expectedFailure);
        row.put("sql", sql);
        row.put("business_facts", Map.of());
        if (failure != null) row.put("failure_type", failure.getClass().getSimpleName());
        if (measurements.putIfAbsent(phase, row) != null)
            throw new IllegalStateException("Duplicate measurement phase");
        return new Observed<>(value, failure, sql, row);
    }

    private static void assertSql(Observed<?> measured, int count, boolean failure) {
        if (failure) assertThat(measured.failure()).isNotNull();
        else assertThat(measured.failure()).isNull();
        assertThat(measured.sql().statements()).as(measured.sql().phase()).isEqualTo(count);
    }

    private static void assertRecovery(Observed<?> measured, boolean failure) {
        assertSql(measured, 1, failure);
        assertThat(measured.sql().count("notice_expired_recovery")).isOne();
        assertThat(measured.sql().executions().getFirst().templateSha256())
                .isEqualTo(RECOVERY_HASH);
        // 生产入口未额外包事务。自动提交 UPDATE 的隐式回滚由完整数据 oracle 证明，不能伪造 JDBC rollback 计数。
    }

    private List<Snapshot> snapshots() {
        return raw.query(
                "SELECT id,turn_id,binding_id,binding_revision,revision,payload,status,reason,started_at::text,created_at::text "
                        + "FROM channel.notices ORDER BY id",
                (rs, n) -> snapshot(rs));
    }

    private List<Snapshot> snapshots(Connection connection) throws java.sql.SQLException {
        var result = new ArrayList<Snapshot>();
        try (var statement = connection.createStatement();
                var rows =
                        statement.executeQuery(
                                "SELECT id,turn_id,binding_id,binding_revision,revision,payload,status,reason,started_at::text,created_at::text "
                                        + "FROM channel.notices ORDER BY id")) {
            while (rows.next()) result.add(snapshot(rows));
        }
        return result;
    }

    private static Snapshot snapshot(java.sql.ResultSet row) throws java.sql.SQLException {
        return new Snapshot(
                new Notice(
                        row.getString(1),
                        row.getString(2),
                        row.getString(3),
                        row.getLong(4),
                        row.getLong(5),
                        row.getString(6),
                        row.getString(7),
                        row.getString(8)),
                row.getString(9),
                row.getString(10));
    }

    private Notice notice(Scenario sample) {
        return snapshots().stream()
                .map(Snapshot::notice)
                .filter(
                        n ->
                                n.turnId().equals(sample.turn().id())
                                        && n.revision() == sample.turn().revision())
                .findFirst()
                .orElseThrow();
    }

    private void assertState(Scenario sample, String status, String reason) {
        var notice = notice(sample);
        assertThat(notice.status()).isEqualTo(status);
        assertThat(notice.reason()).isEqualTo(reason);
        assertThat(notice.payload()).isEqualTo(sample.turn().result());
        assertThat(notice.bindingId()).isEqualTo(sample.binding().id());
        assertThat(notice.bindingRevision()).isEqualTo(sample.binding().revision());
    }

    private static void assertOnlyRecovered(
            List<Snapshot> before, List<Snapshot> after, List<String> ids) {
        var expected =
                before.stream()
                        .map(
                                s -> {
                                    if (!ids.contains(s.notice().id())) return s;
                                    var n = s.notice();
                                    return new Snapshot(
                                            new Notice(
                                                    n.id(),
                                                    n.turnId(),
                                                    n.bindingId(),
                                                    n.bindingRevision(),
                                                    n.revision(),
                                                    n.payload(),
                                                    "unknown",
                                                    "worker_lost"),
                                            s.startedAt(),
                                            s.createdAt());
                                })
                        .toList();
        assertThat(after).isEqualTo(expected);
    }

    private void awaitBlockedBy(int holder) {
        long deadline = System.nanoTime() + Duration.ofSeconds(5).toNanos();
        do {
            if (raw.queryForObject(
                            "SELECT count(*)::int FROM pg_stat_activity a WHERE a.datname=current_database() "
                                    + "AND a.pid<>pg_backend_pid() AND ?=ANY(pg_blocking_pids(a.pid))",
                            Integer.class,
                            holder)
                    > 0) return;
            LockSupport.parkNanos(TimeUnit.MILLISECONDS.toNanos(5));
        } while (System.nanoTime() < deadline);
        throw new AssertionError("Recovery UPDATE did not wait on the held notice row");
    }

    private boolean indexPresent() {
        return Boolean.TRUE.equals(
                raw.queryForObject(
                        "SELECT to_regclass('channel.channel_sending_notice') IS NOT NULL",
                        Boolean.class));
    }

    private void dropFailureTrigger() {
        if (raw == null) return;
        raw.execute("DROP TRIGGER IF EXISTS p3a18_recovery_failure ON channel.notices");
        raw.execute("DROP FUNCTION IF EXISTS channel.p3a18_recovery_failure()");
    }

    private static String digest(String sql) {
        return PayloadDigest.sha256(sql.replaceAll("\\s+", " ").trim().toLowerCase(Locale.ROOT));
    }

    private static void collectNodes(JsonNode node, List<JsonNode> result) {
        result.add(node);
        for (var child : node.path("Plans")) collectNodes(child, result);
    }

    private static List<Map<String, Object>> safeNodes(List<JsonNode> nodes) {
        var safe = new ArrayList<Map<String, Object>>();
        for (var node : nodes) {
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
                    "index_condition_contains_clock_timestamp",
                    node.path("Index Cond").asText().contains("clock_timestamp"));
            row.put(
                    "filter_contains_volatile_clock",
                    node.path("Filter").asText().contains("clock_timestamp"));
            safe.add(row);
        }
        return safe;
    }

    private void closeResources() {
        try {
            if (application != null) application.close();
        } finally {
            postgres.stop();
        }
    }
}
