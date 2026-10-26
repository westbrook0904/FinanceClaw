package com.financeclaw.m1;

import static org.assertj.core.api.Assertions.*;

import com.financeclaw.app.execution.ExecutionLeaseService;
import com.financeclaw.domain.Task.*;
import com.financeclaw.infrastructure.task.MyBatisCommandGateway;
import com.financeclaw.infrastructure.task.TaskMapper;
import java.sql.Connection;
import java.sql.SQLException;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.function.BooleanSupplier;
import org.apache.ibatis.session.Configuration;
import org.apache.ibatis.session.LocalCacheScope;
import org.flywaydb.core.Flyway;
import org.junit.jupiter.api.*;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.CsvSource;
import org.junit.jupiter.params.provider.ValueSource;
import org.mybatis.spring.SqlSessionFactoryBean;
import org.mybatis.spring.SqlSessionTemplate;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DriverManagerDataSource;
import org.springframework.jdbc.support.JdbcTransactionManager;
import org.springframework.transaction.support.TransactionTemplate;
import org.testcontainers.containers.PostgreSQLContainer;

/** 普通命令锁必须在真实行锁之后计算 live。使用明确合成的单命令租约夹具， 仅验证生产 Mapper、Spring 事务与 fenced 回调，不将其解释为 SDK 或吞吐验收。 */
class CommandLeaseLockIT {
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
    private static TaskMapper mapper;
    private static ExecutionLeaseService leases;

    @BeforeAll
    static void start() throws Exception {
        try {
            PG.start();
            source =
                    new DriverManagerDataSource(
                            PG.getJdbcUrl(), PG.getUsername(), PG.getPassword());
            Flyway.configure().dataSource(source).load().migrate();
            sql = new JdbcTemplate(source);
            tx = new TransactionTemplate(new JdbcTransactionManager(source));
            var config = new Configuration();
            config.setMapUnderscoreToCamelCase(true);
            config.setCacheEnabled(false);
            // 此处故意保留 SESSION，确认锁语句本身清除本地缓存；生产配置仍为 STATEMENT。
            config.setLocalCacheScope(LocalCacheScope.SESSION);
            config.addMapper(TaskMapper.class);
            var factory = new SqlSessionFactoryBean();
            factory.setDataSource(source);
            factory.setConfiguration(config);
            mapper = new SqlSessionTemplate(factory.getObject()).getMapper(TaskMapper.class);
            leases = new ExecutionLeaseService(new MyBatisCommandGateway(mapper), null, null, tx);
        } catch (Exception | Error failure) {
            PG.stop();
            throw failure;
        }
    }

    @AfterAll
    static void stop() {
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

    @ParameterizedTest
    @ValueSource(strings = {"id", "turn"})
    void aPureRowLockWaitComputesExpiryAfterTheLockWithoutAnyUpdatedTuple(String lookup)
            throws Exception {
        shortLease();
        var before = sql.queryForList("SELECT * FROM business.commands");
        try (var holder = hold();
                var worker = Executors.newSingleThreadExecutor()) {
            int holderPid = backend(holder);
            var result = worker.submit(() -> tx.execute(status -> read(lookup)));
            try {
                await(() -> blockedBy(holderPid));
                assertThat(expired()).as("wait began while the lease was still live").isFalse();
                await(CommandLeaseLockIT::expired);
            } finally {
                // 不 UPDATE：此用例专门覆盖没有新 tuple 触发重求值的纯行锁等待。
                holder.rollback();
            }
            assertThat(result.get(5, TimeUnit.SECONDS))
                    .isEqualTo(
                            new Command("command", "turn", "running", "attempt", 4, false, null));
        }
        assertThat(sql.queryForList("SELECT * FROM business.commands")).isEqualTo(before);
    }

    @Test
    void aFencedActionDoesNotRunWhenTheCommandExpiresDuringAPureLockWait() throws Exception {
        shortLease();
        var before = sql.queryForList("SELECT * FROM business.commands");
        var invoked = new AtomicBoolean();
        try (var holder = hold();
                var worker = Executors.newSingleThreadExecutor()) {
            int holderPid = backend(holder);
            var result =
                    worker.submit(
                            () ->
                                    leases.fenced(
                                            CLAIM,
                                            () -> {
                                                invoked.set(true);
                                                return "must not run";
                                            }));
            try {
                await(() -> blockedBy(holderPid));
                assertThat(expired())
                        .as("fenced wait began while the lease was still live")
                        .isFalse();
                await(CommandLeaseLockIT::expired);
            } finally {
                holder.rollback();
            }
            assertThatThrownBy(() -> result.get(5, TimeUnit.SECONDS))
                    .isInstanceOf(ExecutionException.class)
                    .hasCauseInstanceOf(Fenced.class);
        }
        assertThat(invoked).isFalse();
        assertThat(sql.queryForList("SELECT * FROM business.commands")).isEqualTo(before);
    }

    @ParameterizedTest
    @CsvSource({"id,renew", "turn,renew", "id,finish", "turn,finish"})
    void aWaitingLockReadReturnsTheCommittedVersionAndItsCurrentLease(String lookup, String change)
            throws Exception {
        try (var holder = hold();
                var worker = Executors.newSingleThreadExecutor()) {
            int holderPid = backend(holder);
            var result = worker.submit(() -> tx.execute(status -> read(lookup)));
            try {
                await(() -> blockedBy(holderPid));
                try (var update = holder.createStatement()) {
                    if (change.equals("renew")) {
                        update.executeUpdate(
                                "UPDATE business.commands SET lease_until=clock_timestamp()+interval '120 seconds' WHERE id='command'");
                    } else {
                        update.executeUpdate(
                                "UPDATE business.commands SET status='done',lease_until=NULL,fence=fence+1 WHERE id='command'");
                    }
                }
                holder.commit();
            } finally {
                holder.rollback();
            }
            var command = result.get(5, TimeUnit.SECONDS);
            assertThat(command)
                    .isEqualTo(
                            change.equals("renew")
                                    ? CLAIM.command()
                                    : new Command(
                                            "command", "turn", "done", "attempt", 5, false, null));
        }
    }

    @ParameterizedTest
    @ValueSource(strings = {"id", "turn"})
    void repeatedLocksWithinOneTransactionUseTheCurrentClockAndDoNotReuseLive(String lookup) {
        shortLease();
        tx.executeWithoutResult(
                status -> {
                    assertThat(read(lookup)).isEqualTo(CLAIM.command());
                    try {
                        await(CommandLeaseLockIT::expired);
                    } catch (InterruptedException failure) {
                        Thread.currentThread().interrupt();
                        throw new IllegalStateException(failure);
                    }
                    assertThat(read(lookup).live()).isFalse();
                });
    }

    @ParameterizedTest
    @ValueSource(strings = {"id", "turn"})
    void anAbsentCommandStillReturnsNull(String lookup) {
        sql.update("DELETE FROM business.commands WHERE id='command'");
        Command command = tx.execute(status -> read(lookup));
        assertThat(command).isNull();
    }

    private static Command read(String lookup) {
        return lookup.equals("id")
                ? mapper.getCommandForUpdate("command")
                : mapper.getCommandByTurnIdForUpdate("turn");
    }

    private static void shortLease() {
        sql.update(
                "UPDATE business.commands SET lease_until=clock_timestamp()+interval '1500 milliseconds' WHERE id='command'");
        assertThat(expired()).isFalse();
    }

    private static boolean expired() {
        return Boolean.TRUE.equals(
                sql.queryForObject(
                        "SELECT lease_until<=clock_timestamp() FROM business.commands WHERE id='command'",
                        Boolean.class));
    }

    private static Connection hold() throws SQLException {
        var connection = source.getConnection();
        connection.setAutoCommit(false);
        try (var statement = connection.createStatement()) {
            statement
                    .executeQuery("SELECT id FROM business.commands WHERE id='command' FOR UPDATE")
                    .close();
            return connection;
        } catch (SQLException failure) {
            connection.close();
            throw failure;
        }
    }

    private static int backend(Connection holder) throws SQLException {
        try (var statement = holder.createStatement();
                var result = statement.executeQuery("SELECT pg_backend_pid()")) {
            assertThat(result.next()).isTrue();
            return result.getInt(1);
        }
    }

    private static boolean blockedBy(int holderPid) {
        return Boolean.TRUE.equals(
                sql.queryForObject(
                        "SELECT EXISTS(SELECT 1 FROM pg_stat_activity a WHERE a.datname=current_database() AND a.pid<>pg_backend_pid() AND ?=ANY(pg_blocking_pids(a.pid)))",
                        Boolean.class,
                        holderPid));
    }

    private static void await(BooleanSupplier ready) throws InterruptedException {
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(5);
        while (!ready.getAsBoolean() && System.nanoTime() < deadline) Thread.sleep(10);
        assertThat(ready.getAsBoolean()).as("database lock/time condition reached").isTrue();
    }
}
