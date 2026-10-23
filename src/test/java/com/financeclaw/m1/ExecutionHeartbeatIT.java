package com.financeclaw.m1;

import static org.assertj.core.api.Assertions.*;

import com.financeclaw.app.execution.ExecutionLeaseService;
import com.financeclaw.domain.Task.*;
import com.financeclaw.infrastructure.task.MyBatisCommandGateway;
import com.financeclaw.infrastructure.task.TaskMapper;
import java.sql.Connection;
import java.sql.SQLException;
import java.time.OffsetDateTime;
import java.util.concurrent.*;
import java.util.function.BooleanSupplier;
import org.apache.ibatis.session.Configuration;
import org.apache.ibatis.session.LocalCacheScope;
import org.flywaydb.core.Flyway;
import org.junit.jupiter.api.*;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.mybatis.spring.SqlSessionFactoryBean;
import org.mybatis.spring.SqlSessionTemplate;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DriverManagerDataSource;
import org.springframework.jdbc.support.JdbcTransactionManager;
import org.springframework.transaction.support.TransactionTemplate;
import org.testcontainers.containers.PostgreSQLContainer;

/**
 * Real row-lock races; a statement-start snapshot must not hide cancellation after lock waiting.
 */
class ExecutionHeartbeatIT {
    private static final PostgreSQLContainer<?> PG =
            new PostgreSQLContainer<>("postgres:16.10-alpine");
    private static final Claim CLAIM =
            new Claim(
                    new Command("command", "turn", "running", "attempt", 4, true, null),
                    new Turn(
                            "turn",
                            "conversation",
                            "tenant",
                            "alice",
                            "key",
                            "hash",
                            "message",
                            "release",
                            "running",
                            1,
                            null,
                            null));
    private static DriverManagerDataSource source;
    private static JdbcTemplate sql;
    private static TransactionTemplate tx;
    private static ExecutionLeaseService leases;

    @BeforeAll
    static void database() throws Exception {
        PG.start();
        source = new DriverManagerDataSource(PG.getJdbcUrl(), PG.getUsername(), PG.getPassword());
        Flyway.configure().dataSource(source).load().migrate();
        sql = new JdbcTemplate(source);
        tx = new TransactionTemplate(new JdbcTransactionManager(source));
        var config = new Configuration();
        config.setMapUnderscoreToCamelCase(true);
        config.setCacheEnabled(false);
        config.setLocalCacheScope(LocalCacheScope.STATEMENT);
        config.addMapper(TaskMapper.class);
        var factory = new SqlSessionFactoryBean();
        factory.setDataSource(source);
        factory.setConfiguration(config);
        var mapper = new SqlSessionTemplate(factory.getObject()).getMapper(TaskMapper.class);
        leases = new ExecutionLeaseService(new MyBatisCommandGateway(mapper), null, null, null, tx);
    }

    @AfterAll
    static void stopDatabase() {
        PG.stop();
    }

    @BeforeEach
    void seed() {
        sql.execute("TRUNCATE business.conversations CASCADE");
        sql.update(
                "INSERT INTO business.conversations(id,tenant,subject) VALUES('conversation','tenant','alice')");
        sql.update(
                "INSERT INTO business.turns(id,conversation_id,tenant,subject,idempotency_key,request_hash,message,release_hash,status) VALUES('turn','conversation','tenant','alice','key','hash','message','release','running')");
        sql.update(
                "INSERT INTO business.commands(id,turn_id,status,attempt_id,fence,lease_until) VALUES('command','turn','running','attempt',4,clock_timestamp()+interval '60 seconds')");
    }

    @Test
    void renewalExtendsTheLeaseAndReportsCancellationFromTheSameStatement() {
        var before = expiry();
        assertThat(leases.heartbeat(CLAIM, 120000)).isFalse();
        assertThat(expiry()).isAfter(before);
        sql.update("UPDATE business.turns SET status='cancelling' WHERE id='turn'");
        assertThat(leases.heartbeat(CLAIM, 120000)).isTrue();
    }

    @ParameterizedTest
    @ValueSource(strings = {"attempt", "fence", "status", "expired", "missing", "turn"})
    void invalidOrExpiredClaimsCannotExtendTheLease(String reason) {
        switch (reason) {
            case "attempt" ->
                    sql.update(
                            "UPDATE business.commands SET attempt_id='replacement' WHERE id='command'");
            case "fence" ->
                    sql.update("UPDATE business.commands SET fence=fence+1 WHERE id='command'");
            case "status" ->
                    sql.update("UPDATE business.commands SET status='done' WHERE id='command'");
            case "expired" ->
                    sql.update(
                            "UPDATE business.commands SET lease_until=clock_timestamp()-interval '1 second' WHERE id='command'");
            case "missing" -> sql.update("DELETE FROM business.commands WHERE id='command'");
            default -> {}
        }
        var claim =
                reason.equals("turn")
                        ? new Claim(
                                new Command(
                                        "command",
                                        "another-turn",
                                        "running",
                                        "attempt",
                                        4,
                                        true,
                                        null),
                                CLAIM.turn())
                        : CLAIM;
        var before = reason.equals("missing") ? null : expiry();

        assertThatThrownBy(() -> leases.heartbeat(claim, 120000)).isInstanceOf(Fenced.class);

        if (!reason.equals("missing")) assertThat(expiry()).isEqualTo(before);
    }

    @Test
    void cancellationCommittedWhileHeartbeatWaitsIsVisibleImmediately() throws Exception {
        try (var held = lock("business.commands", "command");
                var pool = Executors.newSingleThreadExecutor()) {
            var heartbeat = pool.submit(() -> leases.heartbeat(CLAIM, 120000));
            try {
                awaitHeartbeatLock();
                execute(held, "UPDATE business.turns SET status='cancelling' WHERE id='turn'");
            } finally {
                held.commit();
            }
            assertThat(heartbeat.get(5, TimeUnit.SECONDS)).isTrue();
        }
    }

    @ParameterizedTest
    @ValueSource(strings = {"finish", "fence", "attempt"})
    void finishOrReassignmentCommittedWhileWaitingFencesTheOldHeartbeat(String change)
            throws Exception {
        var before = expiry();
        try (var held = lock("business.commands", "command");
                var pool = Executors.newSingleThreadExecutor()) {
            var heartbeat = pool.submit(() -> leases.heartbeat(CLAIM, 120000));
            try {
                awaitHeartbeatLock();
                execute(
                        held,
                        switch (change) {
                            case "finish" ->
                                    "UPDATE business.commands SET status='done',fence=fence+1,lease_until=NULL WHERE id='command'";
                            case "fence" ->
                                    "UPDATE business.commands SET fence=fence+1 WHERE id='command'";
                            default ->
                                    "UPDATE business.commands SET attempt_id='replacement' WHERE id='command'";
                        });
            } finally {
                held.commit();
            }
            assertThatThrownBy(() -> heartbeat.get(5, TimeUnit.SECONDS))
                    .hasCauseInstanceOf(Fenced.class);
            if (change.equals("finish")) assertThat(expiry()).isNull();
            else assertThat(expiry()).isEqualTo(before);
        }
    }

    @ParameterizedTest
    @ValueSource(strings = {"business.commands", "business.turns"})
    void expiryIsRecheckedAfterWaitingForEitherRowLock(String table) throws Exception {
        sql.update(
                "UPDATE business.commands SET lease_until=clock_timestamp()+interval '1500 milliseconds' WHERE id='command'");
        var before = expiry();
        try (var held = lock(table, table.endsWith("commands") ? "command" : "turn");
                var pool = Executors.newSingleThreadExecutor()) {
            var heartbeat = pool.submit(() -> leases.heartbeat(CLAIM, 120000));
            try {
                awaitHeartbeatLock();
                if (table.endsWith("turns")) {
                    // The heartbeat must already own command while it is waiting on Turn.
                    try (var probe = source.getConnection()) {
                        assertThatThrownBy(
                                        () ->
                                                execute(
                                                        probe,
                                                        "SELECT id FROM business.commands WHERE id='command' FOR UPDATE NOWAIT"))
                                .isInstanceOf(SQLException.class)
                                .extracting(error -> ((SQLException) error).getSQLState())
                                .isEqualTo("55P03");
                    }
                }
                await(
                        () ->
                                Boolean.TRUE.equals(
                                        sql.queryForObject(
                                                "SELECT lease_until<=clock_timestamp() FROM business.commands WHERE id='command'",
                                                Boolean.class)));
            } finally {
                held.commit();
            }
            assertThatThrownBy(() -> heartbeat.get(5, TimeUnit.SECONDS))
                    .hasCauseInstanceOf(Fenced.class);
            assertThat(expiry()).isEqualTo(before);
        }
    }

    @Test
    void renewalStillRollsBackWithItsCallingTransaction() {
        var before = expiry();
        assertThatThrownBy(
                        () ->
                                tx.executeWithoutResult(
                                        status -> {
                                            leases.heartbeat(CLAIM, 120000);
                                            throw new IllegalStateException("rollback");
                                        }))
                .hasMessage("rollback");
        assertThat(expiry()).isEqualTo(before);
    }

    private static OffsetDateTime expiry() {
        return sql.queryForObject(
                "SELECT lease_until FROM business.commands WHERE id='command'",
                OffsetDateTime.class);
    }

    private static Connection lock(String table, String id) throws SQLException {
        var connection = source.getConnection();
        connection.setAutoCommit(false);
        try (var statement =
                connection.prepareStatement("SELECT id FROM " + table + " WHERE id=? FOR UPDATE")) {
            statement.setString(1, id);
            statement.execute();
        }
        return connection;
    }

    private static void execute(Connection connection, String query) throws SQLException {
        try (var statement = connection.createStatement()) {
            statement.execute(query);
        }
    }

    private static void awaitHeartbeatLock() throws InterruptedException {
        await(
                () ->
                        Boolean.TRUE.equals(
                                sql.queryForObject(
                                        "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock' AND query LIKE '%WITH locked_command AS MATERIALIZED%')",
                                        Boolean.class)));
    }

    private static void await(BooleanSupplier ready) throws InterruptedException {
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(5);
        while (!ready.getAsBoolean() && System.nanoTime() < deadline) Thread.sleep(10);
        assertThat(ready.getAsBoolean())
                .as("database lock/time condition reached before timeout")
                .isTrue();
    }
}
