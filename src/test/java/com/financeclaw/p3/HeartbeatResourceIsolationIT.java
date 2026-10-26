package com.financeclaw.p3;

import static org.assertj.core.api.Assertions.*;
import static org.awaitility.Awaitility.await;
import static org.mockito.Mockito.*;

import com.financeclaw.app.execution.ExecuteTaskService;
import com.financeclaw.app.execution.ExecutionLeaseService;
import com.financeclaw.app.execution.RecoverTaskService;
import com.financeclaw.app.task.WorkSignals;
import com.financeclaw.bootstrap.FinanceClawApplication;
import com.financeclaw.domain.Task.*;
import com.financeclaw.infrastructure.task.HeartbeatResources;
import com.financeclaw.infrastructure.task.TaskMapper;
import com.financeclaw.worker.TaskWorker;
import com.zaxxer.hikari.HikariDataSource;
import io.micrometer.core.instrument.MeterRegistry;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Proxy;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.SQLException;
import java.time.Duration;
import java.time.OffsetDateTime;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import javax.sql.DataSource;
import org.apache.ibatis.session.SqlSessionFactory;
import org.junit.jupiter.api.*;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.springframework.beans.factory.config.BeanPostProcessor;
import org.springframework.boot.WebApplicationType;
import org.springframework.boot.builder.SpringApplicationBuilder;
import org.springframework.boot.test.context.TestConfiguration;
import org.springframework.context.ConfigurableApplicationContext;
import org.springframework.context.annotation.Bean;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DelegatingDataSource;
import org.springframework.jdbc.datasource.DriverManagerDataSource;
import org.springframework.transaction.PlatformTransactionManager;
import org.testcontainers.containers.PostgreSQLContainer;

/**
 * 真 PostgreSQL + 正常 Spring 装配验证两个池的有界隔离。只有故障 Worker 的外层执行端口为替身；
 * 续租、资源、事务、双锁和数据库状态都是真实实现。不会启动真实模型或声称生产容量/网络时延。 观察器只保留计数，原 SQL 仅在内存逐字核对；无 owner、参数或正文导出。
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class HeartbeatResourceIsolationIT {
    private static final Claim CLAIM = claim("one", "alice");
    private final PostgreSQLContainer<?> pg = new PostgreSQLContainer<>("postgres:16.10-alpine");
    private ConfigurableApplicationContext context;
    private JdbcTemplate sql;
    private DataSource business;
    private HeartbeatResources resources;
    private ExecutionLeaseService leases;
    private MeterRegistry metrics;
    private Observations observed;

    private static Claim claim(String suffix, String subject) {
        return new Claim(
                new Command(
                        "command-" + suffix,
                        "turn-" + suffix,
                        "running",
                        "attempt-" + suffix,
                        4,
                        true,
                        null),
                new Turn(
                        "turn-" + suffix,
                        "conversation-" + suffix,
                        "heartbeat-isolation",
                        subject,
                        "key",
                        "hash",
                        "question",
                        "release",
                        "running",
                        1,
                        null,
                        null));
    }

    static final class Observations {
        final AtomicInteger renewals = new AtomicInteger();
        final AtomicInteger commits = new AtomicInteger();
        final AtomicInteger rollbacks = new AtomicInteger();

        DataSource wrap(DataSource source) {
            return new DelegatingDataSource(source) {
                @Override
                public Connection getConnection() throws SQLException {
                    Connection real = super.getConnection();
                    return (Connection)
                            Proxy.newProxyInstance(
                                    Connection.class.getClassLoader(),
                                    new Class<?>[] {Connection.class},
                                    (proxy, method, args) -> {
                                        try {
                                            Object value = method.invoke(real, args);
                                            if (method.getName().equals("commit"))
                                                commits.incrementAndGet();
                                            if (method.getName().equals("rollback"))
                                                rollbacks.incrementAndGet();
                                            if (method.getName().equals("prepareStatement")
                                                    && value
                                                            instanceof
                                                            PreparedStatement statement) {
                                                String prepared = (String) args[0];
                                                String normalized =
                                                        prepared.replaceAll("\\s+", " ").trim();
                                                String expected =
                                                        TaskMapper.RENEW_COMMAND_LEASE
                                                                .replaceAll("#\\{[^}]+}", "?")
                                                                .replaceAll("\\s+", " ")
                                                                .trim();
                                                assertThat(normalized).isEqualTo(expected);
                                                return Proxy.newProxyInstance(
                                                        PreparedStatement.class.getClassLoader(),
                                                        new Class<?>[] {PreparedStatement.class},
                                                        (p, m, a) -> {
                                                            if (m.getName().startsWith("execute"))
                                                                renewals.incrementAndGet();
                                                            try {
                                                                return m.invoke(statement, a);
                                                            } catch (
                                                                    InvocationTargetException
                                                                            failure) {
                                                                throw failure.getCause();
                                                            }
                                                        });
                                            }
                                            return value;
                                        } catch (InvocationTargetException failure) {
                                            throw failure.getCause();
                                        }
                                    });
                }
            };
        }
    }

    @TestConfiguration(proxyBeanMethods = false)
    static class ObservedResources {
        @Bean
        static Observations observations() {
            return new Observations();
        }

        @Bean
        static BeanPostProcessor observeHeartbeatResources(Observations observed) {
            return new BeanPostProcessor() {
                @Override
                public Object postProcessAfterInitialization(Object bean, String name) {
                    return bean instanceof HeartbeatResources resources
                            ? resources.decorate(observed::wrap)
                            : bean;
                }
            };
        }
    }

    @BeforeAll
    void start() throws Exception {
        pg.start();
        context =
                new SpringApplicationBuilder(FinanceClawApplication.class, ObservedResources.class)
                        .web(WebApplicationType.NONE)
                        .run(
                                "--spring.datasource.url=" + pg.getJdbcUrl(),
                                "--spring.datasource.username=" + pg.getUsername(),
                                "--spring.datasource.password=" + pg.getPassword(),
                                "--spring.datasource.hikari.maximum-pool-size=4",
                                "--spring.config.import=",
                                "--spring.main.banner-mode=off",
                                "--management.endpoint.health.group.readiness.include=db",
                                "--financeclaw.worker.enabled=false",
                                "--financeclaw.worker.heartbeat-pool.connection-timeout-ms=300",
                                "--financeclaw.worker.heartbeat-pool.statement-timeout-ms=4000",
                                "--financeclaw.worker.heartbeat-pool.lock-timeout-ms=3000",
                                "--financeclaw.integrations.enabled=false",
                                "--financeclaw.memory.enabled=false",
                                "--financeclaw.memory.forget-ledger.enabled=false",
                                "--financeclaw.feishu.enabled=false",
                                "--financeclaw.business.hotel.enabled=false",
                                "--financeclaw.business.taibu.enabled=false",
                                "--financeclaw.business.ziwei.enabled=false",
                                "--financeclaw.model.name=offline");
        business = context.getBean(DataSource.class);
        resources = context.getBean(HeartbeatResources.class);
        leases = context.getBean(ExecutionLeaseService.class);
        metrics = context.getBean(MeterRegistry.class);
        observed = context.getBean(Observations.class);
        sql =
                new JdbcTemplate(
                        new DriverManagerDataSource(
                                pg.getJdbcUrl(), pg.getUsername(), pg.getPassword()));
        // 先建立两个私有物理连接，后续窗口的延迟断言不包含首次建连/服务启动。
        try (var first = resources.dataSource().getConnection();
                var second = resources.dataSource().getConnection()) {
            assertThat(first).isNotSameAs(second);
        }
    }

    @AfterAll
    void close() throws Exception {
        try {
            if (context != null) {
                HikariDataSource main = null;
                HikariDataSource heartbeat = null;
                try {
                    if (business != null) main = business.unwrap(HikariDataSource.class);
                    if (resources != null)
                        heartbeat = resources.dataSource().unwrap(HikariDataSource.class);
                } finally {
                    context.close();
                }
                if (main != null) assertThat(main.isClosed()).isTrue();
                if (heartbeat != null) assertThat(heartbeat.isClosed()).isTrue();
            }
        } finally {
            pg.stop();
        }
    }

    @BeforeEach
    void seed() {
        sql.execute("TRUNCATE business.conversations CASCADE");
        seed(CLAIM);
        observed.renewals.set(0);
        observed.commits.set(0);
        observed.rollbacks.set(0);
    }

    private void seed(Claim claim) {
        sql.update(
                "INSERT INTO business.conversations(id,tenant,subject) VALUES(?,?,?)",
                claim.turn().conversationId(),
                claim.turn().tenant(),
                claim.turn().subject());
        sql.update(
                "INSERT INTO business.turns(id,conversation_id,tenant,subject,idempotency_key,"
                        + "request_hash,message,release_hash,status) VALUES(?,?,?,?,?,?,?,?, 'running')",
                claim.turn().id(),
                claim.turn().conversationId(),
                claim.turn().tenant(),
                claim.turn().subject(),
                "key",
                "hash",
                "question",
                "release");
        sql.update(
                "INSERT INTO business.commands(id,turn_id,status,attempt_id,fence,lease_until)"
                        + " VALUES(?,?,'running',?,4,clock_timestamp()+interval '60 seconds')",
                claim.command().id(),
                claim.turn().id(),
                claim.command().attemptId());
    }

    @Test
    void businessPoolSaturationDoesNotPreventHeartbeatOrCreateExtraAutoconfigurationCandidates()
            throws Exception {
        var main = business.unwrap(HikariDataSource.class);
        var heartbeat = resources.dataSource().unwrap(HikariDataSource.class);
        assertThat(main.getMaximumPoolSize()).isEqualTo(2);
        assertThat(heartbeat.getMaximumPoolSize()).isEqualTo(2);
        assertThat(main.getMaximumPoolSize() + heartbeat.getMaximumPoolSize()).isEqualTo(4);
        assertThat(context.getBeansOfType(DataSource.class)).containsOnlyKeys("dataSource");
        assertThat(context.getBeansOfType(PlatformTransactionManager.class)).hasSize(1);
        assertThat(context.getBeansOfType(SqlSessionFactory.class)).hasSize(1);
        var before = expiry();
        try (var first = business.getConnection();
                var second = business.getConnection()) {
            assertThat(main.getHikariPoolMXBean().getActiveConnections()).isEqualTo(2);
            assertThat(leases.heartbeat(CLAIM, 120000)).isFalse();
            assertThat(expiry()).isAfter(before);
            assertThat(observed.renewals).hasValue(1);
            assertThat(observed.commits).hasValue(1);
            assertThat(observed.rollbacks).hasValue(0);
            assertThat(main.getHikariPoolMXBean().getActiveConnections()).isEqualTo(2);
        }
        assertThat(heartbeat.getHikariPoolMXBean().getActiveConnections()).isZero();
    }

    @Test
    void privatePoolSaturationInterruptsActualWorkerWithoutBorrowingBusinessConnection()
            throws Exception {
        var main = business.unwrap(HikariDataSource.class);
        var started = new CountDownLatch(1);
        var interrupted = new CountDownLatch(1);
        var execution = mock(ExecuteTaskService.class);
        doAnswer(
                        call -> {
                            started.countDown();
                            interrupted.await(5, TimeUnit.SECONDS);
                            return null;
                        })
                .when(execution)
                .execute(CLAIM);
        doAnswer(
                        call -> {
                            interrupted.countDown();
                            return null;
                        })
                .when(execution)
                .interrupt(CLAIM);
        var claims = spy(leases);
        doReturn(CLAIM, (Claim) null).when(claims).claim(anyString(), anyLong());
        var before = expiry();
        try (var first = resources.dataSource().getConnection();
                var second = resources.dataSource().getConnection();
                var worker =
                        new TaskWorker(
                                claims,
                                mock(RecoverTaskService.class),
                                execution,
                                new WorkSignals(),
                                true,
                                1,
                                15000,
                                200)) {
            worker.poll();
            assertThat(started.await(3, TimeUnit.SECONDS)).isTrue();
            worker.heartbeat();
            assertThat(interrupted.await(3, TimeUnit.SECONDS)).isTrue();
            verify(execution, atLeastOnce()).interrupt(CLAIM);
            assertThat(main.getHikariPoolMXBean().getActiveConnections()).isZero();
            assertThat(expiry()).isEqualTo(before);
            assertThat(observed.renewals).hasValue(0);
            assertThat(observed.commits).hasValue(0);
        }
    }

    @ParameterizedTest
    @ValueSource(strings = {"business.commands", "business.turns"})
    void lockTimeoutRollsBackAndReturnsThePrivateConnection(String table) throws Exception {
        var before = expiry();
        try (var held = sql.getDataSource().getConnection()) {
            held.setAutoCommit(false);
            try (var statement =
                    held.prepareStatement("SELECT id FROM " + table + " WHERE id=? FOR UPDATE")) {
                statement.setString(
                        1, table.endsWith("commands") ? CLAIM.command().id() : CLAIM.turn().id());
                statement.executeQuery().close();
            }
            long start = System.nanoTime();
            assertThatThrownBy(() -> leases.heartbeat(CLAIM, 120000))
                    .isInstanceOf(RuntimeException.class);
            assertThat(Duration.ofNanos(System.nanoTime() - start))
                    .isLessThan(Duration.ofSeconds(5));
            assertThat(expiry()).isEqualTo(before);
            assertThat(observed.renewals).hasValue(1);
            assertThat(observed.rollbacks).hasValue(1);
            assertThat(observed.commits).hasValue(0);
            assertThat(
                            resources
                                    .dataSource()
                                    .unwrap(HikariDataSource.class)
                                    .getHikariPoolMXBean()
                                    .getActiveConnections())
                    .isZero();
            held.rollback();
        }
        assertThat(leases.heartbeat(CLAIM, 120000)).isFalse();
    }

    @Test
    void oneOwnersLockedCommandDoesNotConsumeTheOtherReservedConnection() throws Exception {
        Claim other = claim("two", "bob");
        seed(other);
        var before =
                sql.queryForObject(
                        "SELECT lease_until FROM business.commands WHERE id=?",
                        OffsetDateTime.class,
                        other.command().id());
        try (var held = sql.getDataSource().getConnection();
                var workers = Executors.newSingleThreadExecutor()) {
            held.setAutoCommit(false);
            int holder;
            try (var statement = held.createStatement();
                    var rows = statement.executeQuery("SELECT pg_backend_pid()")) {
                rows.next();
                holder = rows.getInt(1);
            }
            try (var statement =
                    held.prepareStatement(
                            "SELECT id FROM business.commands WHERE id=? FOR UPDATE")) {
                statement.setString(1, CLAIM.command().id());
                statement.executeQuery().close();
            }
            var waiting = workers.submit(() -> leases.heartbeat(CLAIM, 120000));
            try {
                await().atMost(Duration.ofSeconds(2))
                        .pollInterval(Duration.ofMillis(10))
                        .until(
                                () ->
                                        Boolean.TRUE.equals(
                                                sql.queryForObject(
                                                        "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE ? = ANY(pg_blocking_pids(pid)))",
                                                        Boolean.class,
                                                        holder)));
                assertThat(waiting.isDone()).isFalse();
                assertThat(leases.heartbeat(other, 120000)).isFalse();
                assertThat(
                                sql.queryForObject(
                                        "SELECT lease_until FROM business.commands WHERE id=?",
                                        OffsetDateTime.class,
                                        other.command().id()))
                        .isAfter(before);
                assertThat(waiting.isDone()).isFalse();
            } finally {
                held.commit();
            }
            assertThat(waiting.get(5, TimeUnit.SECONDS)).isFalse();
            assertThat(observed.renewals).hasValue(2);
            assertThat(observed.commits).hasValue(2);
            assertThat(observed.rollbacks).hasValue(0);
        }
    }

    @Test
    void staleFenceRollsBackWhereCancellationReturnsTrueFromCommittedRenewal() {
        var before = expiry();
        sql.update("UPDATE business.commands SET fence=fence+1 WHERE id=?", CLAIM.command().id());
        assertThatThrownBy(() -> leases.heartbeat(CLAIM, 120000)).isInstanceOf(Fenced.class);
        assertThat(expiry()).isEqualTo(before);
        assertThat(observed.rollbacks).hasValue(1);
        assertThat(observed.commits).hasValue(0);
        sql.update("UPDATE business.commands SET fence=4 WHERE id=?", CLAIM.command().id());
        sql.update("UPDATE business.turns SET status='cancelling' WHERE id=?", CLAIM.turn().id());
        assertThat(leases.heartbeat(CLAIM, 120000)).isTrue();
        assertThat(expiry()).isAfter(before);
        assertThat(observed.commits).hasValue(1);
        assertThat(
                        metrics.get("financeclaw.execution.heartbeat")
                                .tag("outcome", "fenced")
                                .timer()
                                .count())
                .isGreaterThanOrEqualTo(1);
        assertThat(
                        metrics.getMeters().stream()
                                .filter(
                                        m ->
                                                m.getId()
                                                        .getName()
                                                        .startsWith(
                                                                "financeclaw.execution.heartbeat"))
                                .flatMap(m -> m.getId().getTags().stream())
                                .map(t -> t.getKey())
                                .distinct())
                .containsExactly("outcome");
    }

    @ParameterizedTest
    @ValueSource(strings = {"attempt", "status", "expired", "missing", "turn"})
    void invalidOrExpiredClaimsAreRolledBackByTheDedicatedTransaction(String reason)
            throws Exception {
        switch (reason) {
            case "attempt" ->
                    sql.update(
                            "UPDATE business.commands SET attempt_id='replacement' WHERE id=?",
                            CLAIM.command().id());
            case "status" ->
                    sql.update(
                            "UPDATE business.commands SET status='done' WHERE id=?",
                            CLAIM.command().id());
            case "expired" ->
                    sql.update(
                            "UPDATE business.commands SET lease_until=clock_timestamp()-interval '1 second' WHERE id=?",
                            CLAIM.command().id());
            case "missing" ->
                    sql.update("DELETE FROM business.commands WHERE id=?", CLAIM.command().id());
            default -> {}
        }
        Claim claim =
                reason.equals("turn")
                        ? new Claim(
                                new Command(
                                        CLAIM.command().id(),
                                        "another-turn",
                                        "running",
                                        CLAIM.command().attemptId(),
                                        4,
                                        true,
                                        null),
                                CLAIM.turn())
                        : CLAIM;
        var before = reason.equals("missing") ? null : expiry();
        assertThatThrownBy(() -> leases.heartbeat(claim, 120000)).isInstanceOf(Fenced.class);
        if (!reason.equals("missing")) assertThat(expiry()).isEqualTo(before);
        assertThat(observed.renewals).hasValue(1);
        assertThat(observed.rollbacks).hasValue(1);
        assertThat(observed.commits).hasValue(0);
        assertThat(
                        resources
                                .dataSource()
                                .unwrap(HikariDataSource.class)
                                .getHikariPoolMXBean()
                                .getActiveConnections())
                .isZero();
    }

    @ParameterizedTest
    @ValueSource(strings = {"business.commands", "business.turns"})
    void dedicatedHeartbeatCannotReviveALeaseThatExpiresWhileWaitingForEitherLock(String table)
            throws Exception {
        sql.update(
                "UPDATE business.commands SET lease_until=clock_timestamp()+interval '1200 milliseconds' WHERE id=?",
                CLAIM.command().id());
        var before = expiry();
        try (var held = sql.getDataSource().getConnection();
                var workers = Executors.newSingleThreadExecutor()) {
            held.setAutoCommit(false);
            int holder;
            try (var statement = held.createStatement();
                    var rows = statement.executeQuery("SELECT pg_backend_pid()")) {
                rows.next();
                holder = rows.getInt(1);
            }
            try (var statement =
                    held.prepareStatement("SELECT id FROM " + table + " WHERE id=? FOR UPDATE")) {
                statement.setString(
                        1, table.endsWith("commands") ? CLAIM.command().id() : CLAIM.turn().id());
                statement.executeQuery().close();
            }
            var waiting = workers.submit(() -> leases.heartbeat(CLAIM, 120000));
            try {
                await().atMost(Duration.ofSeconds(2))
                        .pollInterval(Duration.ofMillis(10))
                        .until(
                                () ->
                                        Boolean.TRUE.equals(
                                                sql.queryForObject(
                                                        "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE ? = ANY(pg_blocking_pids(pid)))",
                                                        Boolean.class,
                                                        holder)));
                await().atMost(Duration.ofSeconds(2))
                        .pollInterval(Duration.ofMillis(10))
                        .until(
                                () ->
                                        Boolean.TRUE.equals(
                                                sql.queryForObject(
                                                        "SELECT lease_until<=clock_timestamp() FROM business.commands WHERE id=?",
                                                        Boolean.class,
                                                        CLAIM.command().id())));
            } finally {
                held.commit();
            }
            assertThatThrownBy(() -> waiting.get(5, TimeUnit.SECONDS))
                    .hasCauseInstanceOf(Fenced.class);
        }
        assertThat(expiry()).isEqualTo(before);
        assertThat(observed.renewals).hasValue(1);
        assertThat(observed.rollbacks).hasValue(1);
        assertThat(observed.commits).hasValue(0);
        assertThat(
                        resources
                                .dataSource()
                                .unwrap(HikariDataSource.class)
                                .getHikariPoolMXBean()
                                .getActiveConnections())
                .isZero();
    }

    @Test
    void sqlFailureRollsBackLeaseAndLeavesNoPrivateConnectionInUse() {
        var before = expiry();
        sql.execute(
                "CREATE FUNCTION business.heartbeat_test_failure() RETURNS trigger LANGUAGE plpgsql AS $$"
                        + "BEGIN RAISE EXCEPTION 'synthetic_heartbeat_failure'; END $$");
        sql.execute(
                "CREATE TRIGGER heartbeat_test_failure AFTER UPDATE ON business.commands"
                        + " FOR EACH STATEMENT EXECUTE FUNCTION business.heartbeat_test_failure()");
        try {
            assertThatThrownBy(() -> leases.heartbeat(CLAIM, 120000))
                    .isInstanceOf(RuntimeException.class);
            assertThat(expiry()).isEqualTo(before);
            assertThat(observed.renewals).hasValue(1);
            assertThat(observed.rollbacks).hasValue(1);
            assertThat(observed.commits).hasValue(0);
        } finally {
            sql.execute("DROP TRIGGER heartbeat_test_failure ON business.commands");
            sql.execute("DROP FUNCTION business.heartbeat_test_failure()");
        }
    }

    @Test
    void statementTimeoutCannotCommitAPartiallyExecutedLeaseUpdate() throws Exception {
        var before = expiry();
        sql.execute(
                "CREATE FUNCTION business.heartbeat_test_delay() RETURNS trigger LANGUAGE plpgsql AS $$"
                        + "BEGIN PERFORM pg_sleep(6); RETURN NEW; END $$");
        sql.execute(
                "CREATE TRIGGER heartbeat_test_delay BEFORE UPDATE ON business.commands"
                        + " FOR EACH ROW EXECUTE FUNCTION business.heartbeat_test_delay()");
        try {
            long started = System.nanoTime();
            assertThatThrownBy(() -> leases.heartbeat(CLAIM, 120000))
                    .isInstanceOf(RuntimeException.class);
            assertThat(Duration.ofNanos(System.nanoTime() - started))
                    .isLessThan(Duration.ofSeconds(6));
            assertThat(expiry()).isEqualTo(before);
            assertThat(observed.renewals).hasValue(1);
            assertThat(observed.rollbacks).hasValue(1);
            assertThat(observed.commits).hasValue(0);
            assertThat(
                            resources
                                    .dataSource()
                                    .unwrap(HikariDataSource.class)
                                    .getHikariPoolMXBean()
                                    .getActiveConnections())
                    .isZero();
        } finally {
            sql.execute("DROP TRIGGER heartbeat_test_delay ON business.commands");
            sql.execute("DROP FUNCTION business.heartbeat_test_delay()");
        }
    }

    private OffsetDateTime expiry() {
        return sql.queryForObject(
                "SELECT lease_until FROM business.commands WHERE id=?",
                OffsetDateTime.class,
                CLAIM.command().id());
    }
}
