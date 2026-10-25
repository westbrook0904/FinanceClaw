package com.financeclaw.p3;

import static org.assertj.core.api.Assertions.*;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.financeclaw.app.execution.ExecutionLeaseService;
import com.financeclaw.app.task.SubmitTaskService;
import com.financeclaw.app.task.port.TaskQueryGateway;
import com.financeclaw.bootstrap.FinanceClawApplication;
import com.financeclaw.domain.ExecutionScope;
import com.financeclaw.domain.Governance.Answer;
import com.financeclaw.domain.Task.*;
import com.financeclaw.support.PayloadJson;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.sql.SQLException;
import java.time.OffsetDateTime;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
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
import org.testcontainers.containers.PostgreSQLContainer;

/**
 * 真实 PostgreSQL 领取事务的 SQL 与锁竞争基线，不启动模型或自动 Worker。
 *
 * <p>发布、受理、故障 trigger 和最终读回在观察窗口外准备；窗口只包含一次生产 ExecutionLeaseService.claim。
 * 并发线程各自开启原有线程内计数器，不能把其他线程的工作归入同一份单次领取样本。本轮专用 baseline 开关只选择 Turn 更新后回读优化前的 7 次或优化后的 6 次
 * SQL，不修改生产语义或数据库状态，before/after 使用相同测试源文件。 历史 P3a.10 的通用 baseline 开关对应更早的 8 次 SQL，必须拒绝混用。
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
@Execution(ExecutionMode.SAME_THREAD)
class WorkerClaimQueryIT {
    private static final Owner OWNER = new Owner("worker-claim-query", "alice");
    private static final long LEASE_MILLIS = 120_000;
    private static final boolean BASELINE =
            Boolean.getBoolean("financeclaw.test.worker-turn-returning-baseline");
    private final PostgreSQLContainer<?> postgres =
            new PostgreSQLContainer<>("postgres:16.10-alpine");
    private final List<Map<String, Object>> measurements = new CopyOnWriteArrayList<>();
    private ConfigurableApplicationContext application;
    private DataSource source;
    private JdbcTemplate sql;
    private JdbcExecutionCounter counter;
    private SubmitTaskService submission;
    private ExecutionLeaseService leases;

    record Admitted(String turn, String command) {}

    record Observed<T>(T value, Throwable failure, JdbcExecutionCounter.Measurement sql) {}

    @TestConfiguration(proxyBeanMethods = false)
    static class CountingConfiguration {
        @Bean
        static JdbcExecutionCounter workerClaimSqlCounter() {
            return new JdbcExecutionCounter();
        }

        @Bean
        static BeanPostProcessor countingDataSource(JdbcExecutionCounter counter) {
            return new BeanPostProcessor() {
                @Override
                public Object postProcessAfterInitialization(Object bean, String name) {
                    return name.equals("dataSource") && bean instanceof DataSource delegate
                            ? counter.wrap(delegate)
                            : bean;
                }
            };
        }
    }

    @BeforeAll
    void start() {
        if (Boolean.getBoolean("financeclaw.test.query-baseline")) {
            throw new IllegalStateException(
                    "Historical query-baseline mode measures 8 statements; use worker-turn-returning-baseline for the current 7-to-6 baseline");
        }
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
            source = application.getBean(DataSource.class);
            sql = new JdbcTemplate(source);
            counter = application.getBean(JdbcExecutionCounter.class);
            submission = application.getBean(SubmitTaskService.class);
            leases = application.getBean(ExecutionLeaseService.class);
        } catch (RuntimeException | Error failure) {
            if (application != null) application.close();
            postgres.stop();
            throw failure;
        }
    }

    @BeforeEach
    void reset() {
        dropFailureTrigger();
        sql.execute(
                "TRUNCATE agent_runtime.states,business.conversations,memory.owners,"
                        + "memory.jobs,memory.operations,memory.audit CASCADE");
        sql.update(
                "UPDATE memory.privacy_ledger_binding SET lineage=?,active=false",
                UUID.randomUUID().toString());
    }

    @AfterEach
    void removeFailureInjection() {
        dropFailureTrigger();
    }

    @AfterAll
    void writeEvidenceAndClose() throws Exception {
        try {
            Path output = Path.of("target/p3a12/worker-turn-returning-sql.json");
            Files.createDirectories(output.getParent());
            new ObjectMapper()
                    .writerWithDefaultPrettyPrinter()
                    .writeValue(
                            output.toFile(),
                            Map.of(
                                    "format",
                                    "worker-turn-returning-sql/1",
                                    "baseline_mode",
                                    BASELINE,
                                    "mode",
                                    BASELINE ? "before" : "after",
                                    "measurement_boundary",
                                    "one production claim transaction",
                                    "sql_semantics",
                                    "JDBC execute calls; preparation/readback excluded",
                                    "expected_success_statements",
                                    expectedSuccessStatements(),
                                    "expected_empty_statements",
                                    1,
                                    "worker_enabled",
                                    false,
                                    "measurements",
                                    List.copyOf(measurements)));
        } finally {
            try {
                if (application != null) application.close();
            } finally {
                postgres.stop();
            }
        }
    }

    @Test
    void successfulClaimReturnsThePersistedAttemptFenceLeaseAndRunningTurn() {
        var admitted = admit();
        var before = databaseTime();
        var observed = observe("claim-success", () -> leases.claim("query-worker", LEASE_MILLIS));
        var after = databaseTime();
        assertSuccessfulClaim(observed, admitted, 1, null, 2);
        var expiry =
                sql.queryForObject(
                        "SELECT lease_until FROM business.commands WHERE id=?",
                        OffsetDateTime.class,
                        admitted.command());
        assertThat(expiry)
                .isAfterOrEqualTo(before.plusNanos(LEASE_MILLIS * 1_000_000))
                .isBeforeOrEqualTo(after.plusNanos(LEASE_MILLIS * 1_000_000));
        assertThat(
                        sql.queryForObject(
                                "SELECT worker_id FROM business.commands WHERE id=?",
                                String.class,
                                admitted.command()))
                .isEqualTo("query-worker");
    }

    @Test
    void emptyQueueUsesOnlyTheCandidateQueryAndDoesNotCreateExecutionFacts() {
        var observed = observe("claim-empty", () -> leases.claim("query-worker", LEASE_MILLIS));
        assertThat(observed.failure()).isNull();
        assertThat(observed.value()).isNull();
        assertThat(observed.sql().statements()).isOne();
        assertThat(observed.sql().families()).containsOnly(entry("command_claim_poll", 1L));
        assertTransaction(observed.sql(), false);
        assertThat(count("business.attempts")).isZero();
        assertThat(count("business.events")).isZero();
    }

    @Test
    void twoWorkersCanCreateOnlyOneAttemptForTheSameQueuedCommand() throws Exception {
        var admitted = admit();
        var start = new CountDownLatch(1);
        Observed<Claim> first;
        Observed<Claim> second;
        try (var workers = Executors.newFixedThreadPool(2)) {
            var one =
                    workers.submit(
                            () -> {
                                assertThat(start.await(10, TimeUnit.SECONDS)).isTrue();
                                return observe(
                                        "race-worker-one",
                                        () -> leases.claim("worker-one", LEASE_MILLIS));
                            });
            var two =
                    workers.submit(
                            () -> {
                                assertThat(start.await(10, TimeUnit.SECONDS)).isTrue();
                                return observe(
                                        "race-worker-two",
                                        () -> leases.claim("worker-two", LEASE_MILLIS));
                            });
            start.countDown();
            first = one.get(15, TimeUnit.SECONDS);
            second = two.get(15, TimeUnit.SECONDS);
        }
        assertThat(first.failure()).isNull();
        assertThat(second.failure()).isNull();
        var samples = List.of(first, second);
        assertThat(samples.stream().filter(s -> s.value() != null)).hasSize(1);
        var winner = samples.stream().filter(s -> s.value() != null).findFirst().orElseThrow();
        var empty = samples.stream().filter(s -> s.value() == null).findFirst().orElseThrow();
        assertSuccessfulClaim(winner, admitted, 1, null, 2);
        assertThat(empty.sql().statements()).isOne();
        assertTransaction(empty.sql(), false);
        assertThat(count("business.attempts")).isOne();
        assertThat(count("business.events")).isEqualTo(2);
    }

    @Test
    void lockedOldestCommandIsSkippedAndCanBeClaimedAfterTheLockIsReleased() throws Exception {
        var oldest = admit();
        var next = admit();
        sql.update(
                "UPDATE business.commands SET created_at=clock_timestamp()-interval '1 minute' WHERE id=?",
                oldest.command());
        try (var held = lockCommand(oldest.command());
                var worker = Executors.newSingleThreadExecutor()) {
            var future =
                    worker.submit(
                            () ->
                                    observe(
                                            "skip-locked-next",
                                            () -> leases.claim("skip-worker", LEASE_MILLIS)));
            assertSuccessfulClaim(future.get(5, TimeUnit.SECONDS), next, 1, null, 2);
            assertThat(command(oldest).get("status")).isEqualTo("queued");
        }
        var released =
                observe(
                        "previously-locked-command",
                        () -> leases.claim("later-worker", LEASE_MILLIS));
        assertSuccessfulClaim(released, oldest, 1, null, 2);
        assertThat(count("business.attempts")).isEqualTo(2);
    }

    @Test
    void resumingClaimPreservesResumeIdAndAdvancesTheStoredFenceExactlyOnce() {
        var admitted = admit();
        String resume = "resume-" + UUID.randomUUID();
        String previousAttempt = UUID.randomUUID().toString();
        var turn = application.getBean(TaskQueryGateway.class).turn(admitted.turn());
        var previous =
                new Claim(
                        new Command(
                                admitted.command(),
                                admitted.turn(),
                                "done",
                                previousAttempt,
                                6,
                                false,
                                null),
                        turn);
        var scope = previous.scope();
        String action = ExecutionScope.hash(List.of("fixture-city-action"));
        String response = PayloadJson.canonical(new Answer("input", 1, "submit", action, "Ningbo"));
        // 只准备领取边界的合法历史元数据：answered interaction 指向已封存 waiting attempt 与同版本 scope。
        // 保留真实外键及身份/版本绑定；该合成检查点不证明原生 SDK 恢复或真实人工回答流程。
        sql.update(
                "INSERT INTO business.attempts(id,command_id,fence,state_version,outcome) VALUES(?,?,6,1,'waiting')",
                previousAttempt,
                admitted.command());
        sql.update(
                "INSERT INTO agent_runtime.states(namespace,user_id,session_id,state_key,version,payload)"
                        + " VALUES(?,?,?,'agent_state',1,'{}'::jsonb)",
                scope.namespace(),
                scope.userId(),
                scope.sessionId());
        sql.update(
                "INSERT INTO business.interactions(id,turn_id,command_id,attempt_id,state_version,grant_revision,release_hash,"
                        + "kind,call_id,tool,action_hash,schema_json,question,status,response_hash,response_json)"
                        + " SELECT ?,t.id,?,?,1,p.grant_revision,t.release_hash,'input','resume-call','request_city',?,"
                        + "'{\"type\":\"string\"}','City?','answered',?,? FROM business.turns t"
                        + " JOIN business.turn_policies p ON p.turn_id=t.id WHERE t.id=?",
                resume,
                admitted.command(),
                previousAttempt,
                action,
                ExecutionScope.hash(List.of(response)),
                response,
                admitted.turn());
        sql.update(
                "UPDATE business.turns SET status='resuming',revision=7 WHERE id=?",
                admitted.turn());
        sql.update(
                "UPDATE business.commands SET fence=7,resume_id=? WHERE id=?",
                resume,
                admitted.command());
        var observed = observe("claim-resuming", () -> leases.claim("resume-worker", LEASE_MILLIS));
        assertSuccessfulClaim(observed, admitted, 8, resume, 8);
        assertThat(count("business.attempts")).isEqualTo(2);
        assertThat(
                        sql.queryForObject(
                                "SELECT count(*) FROM business.interactions i JOIN business.attempts a ON a.id=i.attempt_id"
                                        + " WHERE i.id=? AND i.turn_id=? AND i.command_id=? AND i.status='answered'"
                                        + " AND i.state_version=a.state_version AND a.outcome='waiting' AND a.fence=6",
                                Integer.class,
                                resume,
                                admitted.turn(),
                                admitted.command()))
                .isOne();
    }

    @ParameterizedTest
    @ValueSource(strings = {"attempt", "turn", "event"})
    void failedAttemptTurnUpdateOrRunningEventWriteRollsBackTheWholeClaim(String stage) {
        var admitted = admit();
        var queued = application.getBean(TaskQueryGateway.class).turn(admitted.turn());
        installFailureTrigger(stage);
        var failed =
                observe(
                        "claim-rollback-" + stage,
                        () -> leases.claim("fault-worker", LEASE_MILLIS));
        assertThat(failed.failure())
                .isNotNull()
                .hasStackTraceContaining("worker_claim_query_injected_failure");
        // 三个失败点都在原末尾 Turn SELECT 之前；不能跟随成功路径 7→6 一起减一。
        assertThat(failed.sql().statements())
                .isEqualTo(
                        switch (stage) {
                            case "attempt" -> 4;
                            case "turn" -> 5;
                            case "event" -> 6;
                            default ->
                                    throw new IllegalArgumentException(
                                            "Unknown claim failure stage");
                        });
        assertThat(failed.sql().count("command_lock")).isZero();
        assertThat(failed.sql().count("turn_read")).isOne();
        assertTransaction(failed.sql(), true);
        assertQueuedWithoutAttempt(admitted);
        assertThat(application.getBean(TaskQueryGateway.class).turn(admitted.turn()))
                .isEqualTo(queued);
        assertThat(count("business.events")).isOne();

        dropFailureTrigger();
        var retried =
                observe(
                        "claim-after-rollback-" + stage,
                        () -> leases.claim("retry-worker", LEASE_MILLIS));
        assertSuccessfulClaim(retried, admitted, 1, null, 2);
    }

    @ParameterizedTest
    @ValueSource(strings = {"waiting", "blocked"})
    void queuedCommandCannotClaimANonClaimableTurn(String status) {
        var admitted = admit();
        sql.update("UPDATE business.turns SET status=? WHERE id=?", status, admitted.turn());
        var failed =
                observe(
                        "claim-invalid-turn-" + status,
                        () -> leases.claim("query-worker", LEASE_MILLIS));
        assertThat(failed.failure())
                .isInstanceOf(IllegalStateException.class)
                .hasMessage("Command/Turn mismatch");
        assertThat(failed.sql().statements()).isEqualTo(2);
        assertTransaction(failed.sql(), true);
        assertThat(command(admitted).get("status")).isEqualTo("queued");
        assertThat(command(admitted).get("fence")).isEqualTo(0L);
        assertThat(count("business.attempts")).isZero();
        assertThat(count("business.events")).isOne();
    }

    private Admitted admit() {
        String conversation = submission.createConversation(OWNER);
        var accepted =
                submission.submit(
                        OWNER,
                        conversation,
                        UUID.randomUUID().toString(),
                        "Claim fixture question");
        String command =
                sql.queryForObject(
                        "SELECT id FROM business.commands WHERE turn_id=?",
                        String.class,
                        accepted.turnId());
        return new Admitted(accepted.turnId(), command);
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
        measurements.add(
                Map.of(
                        "phase",
                        phase,
                        "completed_without_exception",
                        failure == null,
                        "claimed",
                        value instanceof Claim,
                        "sql",
                        sample));
        return new Observed<>(value, failure, sample);
    }

    private void assertSuccessfulClaim(
            Observed<Claim> observed,
            Admitted admitted,
            long fence,
            String resumeId,
            long revision) {
        assertThat(observed.failure()).isNull();
        var claimed = observed.value();
        assertThat(claimed).isNotNull();
        assertThat(claimed.command().id()).isEqualTo(admitted.command());
        assertThat(claimed.command().turnId()).isEqualTo(admitted.turn());
        assertThat(claimed.command().status()).isEqualTo("running");
        assertThat(claimed.command().fence()).isEqualTo(fence);
        assertThat(claimed.command().live()).isTrue();
        assertThat(claimed.command().resumeId()).isEqualTo(resumeId);
        assertThat(claimed.command().attemptId()).isNotBlank();
        assertThat(claimed.turn().id()).isEqualTo(admitted.turn());
        assertThat(claimed.turn().status()).isEqualTo("running");
        assertThat(claimed.turn().revision()).isEqualTo(revision);
        // 在计数窗口关闭后读取数据库完整 record；不按构造器参数个数手工挑选字段，避免漏掉身份或正文映射。
        var persistedTurn = application.getBean(TaskQueryGateway.class).turn(admitted.turn());
        assertThat(claimed.turn()).isEqualTo(persistedTurn);
        var command = command(admitted);
        assertThat(command.get("attempt_id")).isEqualTo(claimed.command().attemptId());
        assertThat(command.get("fence")).isEqualTo(fence);
        assertThat(command.get("resume_id")).isEqualTo(resumeId);
        var attempt =
                sql.queryForMap(
                        "SELECT command_id,fence,resume_id FROM business.attempts WHERE id=?",
                        claimed.command().attemptId());
        assertThat(attempt.get("command_id")).isEqualTo(admitted.command());
        assertThat(attempt.get("fence")).isEqualTo(fence);
        assertThat(attempt.get("resume_id")).isEqualTo(resumeId);
        assertThat(
                        sql.queryForObject(
                                "SELECT count(*) FROM business.events WHERE turn_id=? AND status='running' AND revision=?",
                                Integer.class,
                                admitted.turn(),
                                revision))
                .isOne();
        var runningEvent =
                sql.queryForMap(
                        "SELECT turn_id,revision,status,reason FROM business.events WHERE turn_id=? AND status='running'",
                        admitted.turn());
        assertThat(runningEvent.get("turn_id")).isEqualTo(claimed.turn().id());
        assertThat(runningEvent.get("revision")).isEqualTo(claimed.turn().revision());
        assertThat(runningEvent.get("status")).isEqualTo(claimed.turn().status());
        assertThat(runningEvent.get("reason")).isEqualTo(claimed.turn().reason());
        assertThat(observed.sql().statements()).isEqualTo(expectedSuccessStatements());
        assertThat(observed.sql().count("command_lock")).isZero();
        assertThat(observed.sql().count("turn_read")).isEqualTo(BASELINE ? 2 : 1);
        for (String family :
                List.of(
                        "command_claim_poll",
                        "command_claim_update",
                        "attempt_insert",
                        "turn_state_update",
                        "event_insert")) {
            assertThat(observed.sql().count(family)).as(family).isOne();
        }
        assertThat(observed.sql().count("other")).isZero();
        assertTransaction(observed.sql(), false);
    }

    private void assertQueuedWithoutAttempt(Admitted admitted) {
        var command = command(admitted);
        assertThat(command.get("status")).isEqualTo("queued");
        assertThat(command.get("fence")).isEqualTo(0L);
        assertThat(command.get("attempt_id")).isNull();
        assertThat(command.get("worker_id")).isNull();
        assertThat(command.get("lease_until")).isNull();
        assertThat(count("business.attempts")).isZero();
        assertThat(
                        sql.queryForObject(
                                "SELECT status FROM business.turns WHERE id=?",
                                String.class,
                                admitted.turn()))
                .isEqualTo("queued");
        assertThat(
                        sql.queryForObject(
                                "SELECT revision FROM business.turns WHERE id=?",
                                Long.class,
                                admitted.turn()))
                .isEqualTo(1L);
    }

    private Map<String, Object> command(Admitted admitted) {
        return sql.queryForMap("SELECT * FROM business.commands WHERE id=?", admitted.command());
    }

    private static int expectedSuccessStatements() {
        return BASELINE ? 7 : 6;
    }

    private static void assertTransaction(
            JdbcExecutionCounter.Measurement measured, boolean rollback) {
        assertThat(
                        measured.executions().stream()
                                .map(JdbcExecutionCounter.SqlExecution::connection)
                                .distinct())
                .hasSize(1);
        assertThat(measured.commits()).isEqualTo(rollback ? 0 : 1);
        assertThat(measured.rollbacks()).isEqualTo(rollback ? 1 : 0);
    }

    private int count(String table) {
        // 表名只来自本测试的常量，业务 owner、正文与 bind 值不进入报告。
        return sql.queryForObject("SELECT count(*) FROM " + table, Integer.class);
    }

    private OffsetDateTime databaseTime() {
        return sql.queryForObject("SELECT clock_timestamp()", OffsetDateTime.class);
    }

    private Connection lockCommand(String id) throws SQLException {
        var connection = source.getConnection();
        connection.setAutoCommit(false);
        try (var statement =
                connection.prepareStatement(
                        "SELECT id FROM business.commands WHERE id=? FOR UPDATE")) {
            statement.setString(1, id);
            statement.executeQuery().close();
        } catch (SQLException failure) {
            connection.close();
            throw failure;
        }
        return connection;
    }

    private void installFailureTrigger(String stage) {
        String table =
                switch (stage) {
                    case "attempt" -> "business.attempts";
                    case "turn" -> "business.turns";
                    case "event" -> "business.events";
                    default -> throw new IllegalArgumentException("Unknown claim failure stage");
                };
        // Turn 使用 AFTER UPDATE：数据库确实执行过迁移后再报错，验证 command/attempt/Turn 整体回滚。
        String operation = stage.equals("turn") ? "AFTER UPDATE" : "BEFORE INSERT";
        String predicate = stage.equals("attempt") ? "" : " WHEN (NEW.status='running')";
        sql.execute(
                "CREATE FUNCTION business.worker_claim_query_fail() RETURNS trigger LANGUAGE plpgsql AS $$"
                        + " BEGIN RAISE EXCEPTION 'worker_claim_query_injected_failure'; END; $$");
        sql.execute(
                "CREATE TRIGGER worker_claim_query_fail "
                        + operation
                        + " ON "
                        + table
                        + " FOR EACH ROW"
                        + predicate
                        + " EXECUTE FUNCTION business.worker_claim_query_fail()");
    }

    private void dropFailureTrigger() {
        sql.execute("DROP FUNCTION IF EXISTS business.worker_claim_query_fail() CASCADE");
    }
}
