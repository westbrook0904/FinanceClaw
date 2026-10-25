package com.financeclaw.p3;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.*;

import com.fasterxml.jackson.databind.ObjectMapper;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.SQLException;
import java.sql.Statement;
import java.time.Duration;
import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.springframework.jdbc.datasource.AbstractDataSource;

/** 生命周期用可控的 JDBC stub 验证；这些单测不声称 PostgreSQL、连接池或业务端到端验收。 */
class ApplicationJdbcRecorderTest {
    private static final Duration WAIT = Duration.ofSeconds(10);
    private static final String READ = "SELECT id FROM business.turns WHERE id=?";
    private static final String HEARTBEAT =
            "WITH locked_command AS MATERIALIZED (SELECT id FROM business.commands FOR UPDATE),"
                    + " locked_turn AS MATERIALIZED (SELECT id FROM business.turns)"
                    + " UPDATE business.commands c SET lease_until=clock_timestamp()"
                    + " FROM locked_turn t WHERE c.id=t.id RETURNING false";

    @Test
    void releaseSelfJoinCountsOneExecutionInsteadOfTwoTableReads() throws Exception {
        String lookup =
                "SELECT r.release_hash, r.manifest_format, r.manifest::text AS manifest_json,"
                        + " b.release_hash AS base_release_hash, b.manifest_format AS base_manifest_format,"
                        + " b.manifest::text AS base_manifest_json"
                        + " FROM control_plane.release_manifests r LEFT JOIN control_plane.release_manifests b"
                        + " ON r.manifest_format IN ('agent-v2','agent-v3')"
                        + " AND b.release_hash=r.manifest->>'baseReleaseHash' WHERE r.release_hash=?";
        var recorder = new ApplicationJdbcRecorder();
        var database = new Database();
        var window = recorder.begin("joined-release");
        try (var connection = recorder.wrap(database.source).getConnection();
                var statement = connection.prepareStatement(lookup)) {
            statement.executeQuery();
        }
        var result = window.finish(WAIT);
        assertThat(result.statements()).isOne();
        assertThat(result.count("release_lookup")).isOne();
        assertThat(result.families()).containsOnly(entry("release_lookup", 1L));
    }

    @Test
    void capturesAllThreadsWithFixedRolesAndObservesHeartbeatExecution() throws Exception {
        var recorder = new ApplicationJdbcRecorder();
        var database = new Database();
        var wrapped = recorder.wrap(database.source);
        var window = recorder.begin("parallel-origins");
        var start = new CountDownLatch(1);
        List<String> names =
                List.of(
                        "http-nio-private-name",
                        "task-worker-private-name",
                        "task-heartbeat-private-name",
                        "boundedElastic-private-name",
                        "background-private-name");
        try (var executor = Executors.newVirtualThreadPerTaskExecutor()) {
            var futures =
                    names.stream()
                            .map(
                                    name ->
                                            executor.submit(
                                                    () -> {
                                                        Thread.currentThread().setName(name);
                                                        await(start);
                                                        try (var connection =
                                                                        wrapped.getConnection();
                                                                var statement =
                                                                        connection.prepareStatement(
                                                                                name.startsWith(
                                                                                                "task-heartbeat-")
                                                                                        ? HEARTBEAT
                                                                                        : READ)) {
                                                            statement.executeUpdate();
                                                            connection.commit();
                                                        }
                                                        return null;
                                                    }))
                            .toList();
            start.countDown();
            assertThat(window.awaitSqlFamily("heartbeat_renew", 1, WAIT)).isTrue();
            for (var future : futures) future.get(10, TimeUnit.SECONDS);
        }
        var result = window.finish(WAIT);
        assertThat(result.statements()).isEqualTo(5);
        assertThat(result.count("turn_read")).isEqualTo(4);
        assertThat(result.count("heartbeat_renew")).isOne();
        assertThat(result.roles())
                .containsOnly(
                        entry("http", 1L),
                        entry("task_worker", 1L),
                        entry("task_heartbeat", 1L),
                        entry("execution_async", 1L),
                        entry("background", 1L));
        assertThat(result.commits()).isEqualTo(5);
        assertThat(result.borrows().successes()).isEqualTo(5);
        assertThat(result.executions()).allMatch(ApplicationJdbcRecorder.SqlExecution::success);
        assertThat(result.executions())
                .extracting(ApplicationJdbcRecorder.SqlExecution::connection)
                .doesNotHaveDuplicates();
        assertThat(window.finish(Duration.ZERO)).isSameAs(result);
    }

    @Test
    void closingCaptureStillCountsLateSqlAndCommitUntilLogicalConnectionClose() throws Exception {
        var recorder = new ApplicationJdbcRecorder();
        var database = new Database();
        var wrapped = recorder.wrap(database.source);
        var window = recorder.begin("whole-transaction");
        var retained = wrapped.getConnection();
        retained.prepareStatement(READ).executeUpdate();

        var timeout =
                catchThrowableOfType(
                        () -> window.finish(Duration.ZERO),
                        ApplicationJdbcRecorder.RecorderException.class);
        assertThat(timeout.reason()).isEqualTo("drain_timeout");
        assertThat(timeout.diagnostics().activeConnections()).isOne();
        assertThatThrownBy(() -> recorder.begin("too-early"))
                .isInstanceOf(ApplicationJdbcRecorder.RecorderException.class)
                .hasMessageContaining("unfinished_window");

        // 捕获已经关闭，新借连接不属于旧窗口；旧连接仍保留完整的事务生命周期。
        try (var excluded = wrapped.getConnection()) {
            excluded.prepareStatement(READ).executeUpdate();
            excluded.commit();
        }
        retained.prepareStatement(READ).executeUpdate();
        retained.commit();
        assertThatThrownBy(() -> window.finish(Duration.ZERO))
                .hasMessageContaining("drain_timeout");
        retained.close();
        var result = window.finish(WAIT);
        assertThat(result.statements()).isEqualTo(2);
        assertThat(result.commits()).isOne();
        assertThat(result.borrows().attempts()).isOne();

        var next = recorder.begin("next-sample");
        try (var connection = wrapped.getConnection()) {
            connection.prepareStatement(READ).executeUpdate();
        }
        assertThat(next.finish(WAIT).statements()).isOne();
        assertThat(result.statements()).isEqualTo(2);
    }

    @Test
    void retainsBorrowAtEntryEvenIfGetConnectionReturnsAfterCaptureCloses() throws Exception {
        var recorder = new ApplicationJdbcRecorder();
        var database = new Database();
        var entered = new CountDownLatch(1);
        var release = new CountDownLatch(1);
        var source =
                new StubDataSource(
                        () -> {
                            entered.countDown();
                            await(release);
                            return database.connection();
                        });
        var wrapped = recorder.wrap(source);
        var window = recorder.begin("slow-borrow");
        try (var executor = Executors.newVirtualThreadPerTaskExecutor()) {
            var borrowed = executor.submit(() -> wrapped.getConnection());
            try {
                await(entered);
                var timeout =
                        catchThrowableOfType(
                                () -> window.finish(Duration.ZERO),
                                ApplicationJdbcRecorder.RecorderException.class);
                assertThat(timeout.diagnostics().pendingBorrows()).isOne();
                assertThat(timeout.diagnostics().activeConnections()).isZero();
            } finally {
                release.countDown();
            }
            try (var connection = borrowed.get(10, TimeUnit.SECONDS)) {
                connection.prepareStatement(READ).executeUpdate();
                connection.commit();
            }
        }
        var result = window.finish(WAIT);
        assertThat(result.statements()).isOne();
        assertThat(result.commits()).isOne();
        assertThat(result.borrows().attempts()).isOne();
        assertThat(result.borrows().successes()).isOne();
        assertThat(result.borrows().failures()).isZero();
        assertThat(result.borrows().elapsedNanos()).isPositive();
        assertThat(result.borrows().maxElapsedNanos()).isEqualTo(result.borrows().elapsedNanos());
    }

    @Test
    void failedBorrowPreservesOriginalExceptionAndReleasesItsWindowReference() throws Exception {
        var recorder = new ApplicationJdbcRecorder();
        var failure = new SQLException("private connection detail");
        var source =
                recorder.wrap(
                        new StubDataSource(
                                () -> {
                                    throw failure;
                                }));
        var window = recorder.begin("failed-borrow");
        assertThat(catchThrowable(source::getConnection)).isSameAs(failure);
        var result = window.finish(Duration.ZERO);
        assertThat(result.statements()).isZero();
        assertThat(result.borrows().attempts()).isOne();
        assertThat(result.borrows().failures()).isOne();
        assertThat(result.borrows().successes()).isZero();
        assertThat(result.toString()).doesNotContain("private connection detail");
    }

    @Test
    void preWindowPreparedStatementPinsAtFirstExecutionAndCannotMoveToAnotherSample()
            throws Exception {
        var recorder = new ApplicationJdbcRecorder();
        var database = new Database();
        var wrapped = recorder.wrap(database.source);
        var connection = wrapped.getConnection();
        var statement = connection.prepareStatement(READ);
        var window = recorder.begin("pre-window-connection");
        statement.executeUpdate();
        assertThatThrownBy(() -> window.finish(Duration.ZERO))
                .hasMessageContaining("drain_timeout");
        connection.commit();
        connection.close();
        var first = window.finish(WAIT);
        assertThat(first.statements()).isOne();
        assertThat(first.commits()).isOne();
        assertThat(first.borrows().attempts()).isZero();

        var next = recorder.begin("closed-connection-does-not-rebind");
        // 此 stub 容许 closed connection 上继续调用，以专门验证 recorder 不将旧对象重绑定新窗口。
        statement.executeUpdate();
        assertThat(next.finish(Duration.ZERO).statements()).isZero();
        assertThat(first.statements()).isOne();
    }

    @Test
    void finishWaitsForInFlightExecutionEvenWhenAnotherThreadClosesItsConnection()
            throws Exception {
        var recorder = new ApplicationJdbcRecorder();
        var database = new Database();
        var source = recorder.wrap(database.source);
        var window = recorder.begin("in-flight-sql");
        var connection = source.getConnection();
        var entered = new CountDownLatch(1);
        var release = new CountDownLatch(1);
        var raw = mock(PreparedStatement.class);
        when(raw.executeUpdate())
                .thenAnswer(
                        ignored -> {
                            entered.countDown();
                            await(release);
                            return 1;
                        });
        when(database.connections.getFirst().prepareStatement(READ)).thenReturn(raw);
        var statement = connection.prepareStatement(READ);
        try (var executor = Executors.newVirtualThreadPerTaskExecutor()) {
            var executing = executor.submit(() -> statement.executeUpdate());
            try {
                await(entered);
                assertThat(window.awaitSqlFamily("turn_read", 1, Duration.ZERO)).isFalse();
                connection.close();
                var timeout =
                        catchThrowableOfType(
                                () -> window.finish(Duration.ZERO),
                                ApplicationJdbcRecorder.RecorderException.class);
                assertThat(timeout.diagnostics().activeConnections()).isZero();
                assertThat(timeout.diagnostics().inFlight()).isOne();
            } finally {
                release.countDown();
            }
            assertThat(executing.get(10, TimeUnit.SECONDS)).isOne();
        }
        assertThat(window.awaitSqlFamily("turn_read", 1, WAIT)).isTrue();
        assertThat(window.finish(WAIT).statements()).isOne();
    }

    @Test
    void finishWaitsForCommitCompletionAfterConnectionClose() throws Exception {
        var recorder = new ApplicationJdbcRecorder();
        var database = new Database();
        var source = recorder.wrap(database.source);
        var window = recorder.begin("in-flight-commit");
        var connection = source.getConnection();
        connection.prepareStatement(READ).executeUpdate();
        var entered = new CountDownLatch(1);
        var release = new CountDownLatch(1);
        doAnswer(
                        ignored -> {
                            entered.countDown();
                            await(release);
                            return null;
                        })
                .when(database.connections.getFirst())
                .commit();
        try (var executor = Executors.newVirtualThreadPerTaskExecutor()) {
            var committing =
                    executor.submit(
                            () -> {
                                connection.commit();
                                return null;
                            });
            try {
                await(entered);
                connection.close();
                var timeout =
                        catchThrowableOfType(
                                () -> window.finish(Duration.ZERO),
                                ApplicationJdbcRecorder.RecorderException.class);
                assertThat(timeout.diagnostics().inFlight()).isOne();
            } finally {
                release.countDown();
            }
            committing.get(10, TimeUnit.SECONDS);
        }
        assertThat(window.finish(WAIT).commits()).isOne();
    }

    @Test
    void jdbcFailuresAreCountedWithoutReplacingExceptionsOrLeakingTheirMessages() throws Exception {
        var recorder = new ApplicationJdbcRecorder();
        var database = new Database();
        var source = recorder.wrap(database.source);
        var window = recorder.begin("failed-operations");
        var connection = source.getConnection();
        var sqlFailure = new SQLException("private SQL failure");
        var commitFailure = new SQLException("private commit failure");
        var rollbackFailure = new SQLException("private rollback failure");
        var rawStatement = mock(PreparedStatement.class);
        when(rawStatement.executeUpdate()).thenThrow(sqlFailure);
        var rawConnection = database.connections.getFirst();
        when(rawConnection.prepareStatement(READ)).thenReturn(rawStatement);
        doThrow(commitFailure).when(rawConnection).commit();
        doThrow(rollbackFailure).when(rawConnection).rollback();
        var statement = connection.prepareStatement(READ);
        assertThat(catchThrowable(statement::executeUpdate)).isSameAs(sqlFailure);
        assertThat(catchThrowable(connection::commit)).isSameAs(commitFailure);
        assertThat(catchThrowable(connection::rollback)).isSameAs(rollbackFailure);
        connection.close();
        var result = window.finish(WAIT);
        assertThat(result.statements()).isOne();
        assertThat(result.executions().getFirst().success()).isFalse();
        assertThat(result.commits()).isZero();
        assertThat(result.rollbacks()).isZero();
        assertThat(result.commitFailures()).isOne();
        assertThat(result.rollbackFailures()).isOne();
        assertThat(result.toString()).doesNotContain("private");
    }

    @ParameterizedTest
    @ValueSource(booleans = {false, true})
    void overflowingExecutionOrTemplateCapDoesNotInterruptJdbcButInvalidatesTheSample(
            boolean templateCap) throws Exception {
        var recorder =
                new ApplicationJdbcRecorder(
                        templateCap
                                ? new ApplicationJdbcRecorder.Limits(10, 1)
                                : new ApplicationJdbcRecorder.Limits(1, 10));
        var database = new Database();
        var source = recorder.wrap(database.source);
        var window = recorder.begin("bounded-recording");
        try (var connection = source.getConnection()) {
            assertThat(connection.prepareStatement(READ).executeUpdate()).isOne();
            assertThat(connection.prepareStatement(READ + " AND revision=?").executeUpdate())
                    .isOne();
            connection.commit();
        }
        verify(database.connections.getFirst()).commit();
        verify(database.connections.getFirst()).close();
        assertThatThrownBy(() -> window.awaitSqlFamily("turn_read", 1, Duration.ZERO))
                .hasMessageContaining("recording_overflow");
        var failure =
                catchThrowableOfType(
                        () -> window.finish(WAIT), ApplicationJdbcRecorder.RecorderException.class);
        assertThat(failure.reason()).isEqualTo("recording_overflow");
        assertThat(failure.diagnostics().overflow()).isTrue();
        assertThat(failure.diagnostics().activeConnections()).isZero();
        assertThatThrownBy(() -> recorder.begin("cannot-hide-overflow"))
                .hasMessageContaining("unfinished_window");
    }

    @Test
    void closeFailureCanBeRetriedButCannotProduceACertifiedMeasurement() throws Exception {
        var recorder = new ApplicationJdbcRecorder();
        var database = new Database();
        var source = recorder.wrap(database.source);
        var window = recorder.begin("failed-close");
        var connection = source.getConnection();
        var failure = new SQLException("private pool detail");
        doThrow(failure).doNothing().when(database.connections.getFirst()).close();
        assertThat(catchThrowable(connection::close)).isSameAs(failure);
        assertThatThrownBy(() -> window.finish(Duration.ZERO))
                .hasMessageContaining("drain_timeout");
        connection.close();
        var refused =
                catchThrowableOfType(
                        () -> window.finish(WAIT), ApplicationJdbcRecorder.RecorderException.class);
        assertThat(refused.reason()).isEqualTo("connection_close_failed");
        assertThat(refused.diagnostics().closeFailures()).isOne();
        assertThat(refused).hasMessageNotContaining("private pool detail");
    }

    @Test
    void wrappersPreserveConnectionIdentityAndCloseTheOwnedPoolOnlyOnce() throws Exception {
        var recorder = new ApplicationJdbcRecorder();
        var database = new Database();
        var source = recorder.wrap(database.source);
        assertThat(recorder.wrap(source)).isSameAs(source);
        var window = recorder.begin("resource-identity");
        var connection = source.getConnection();
        var statement = connection.createStatement();
        assertThat(statement.getConnection()).isSameAs(connection);
        assertThat(connection.unwrap(Connection.class)).isSameAs(connection);
        assertThat(statement.unwrap(Statement.class)).isSameAs(statement);
        statement.executeUpdate(READ);
        statement.executeBatch();
        connection.rollback();
        connection.close();
        connection.close();
        var result = window.finish(WAIT);
        assertThat(result.statements()).isEqualTo(2);
        assertThat(result.count("jdbc_batch")).isOne();
        assertThat(result.rollbacks()).isOne();
        verify(database.connections.getFirst()).close();
        ((AutoCloseable) source).close();
        ((AutoCloseable) source).close();
        assertThat(database.source.closes.get()).isOne();
    }

    @Test
    void serializedMeasurementContainsOnlyHashesFixedRolesAndBoundedMetadata() throws Exception {
        var recorder = new ApplicationJdbcRecorder();
        var database = new Database();
        var source = recorder.wrap(database.source);
        var window = recorder.begin("sanitized-report");
        try (var connection = source.getConnection();
                var statement = connection.prepareStatement(READ + " AND 'private-sql-body'=?")) {
            statement.setString(1, "private-bind-value");
            statement.executeUpdate();
        }
        var result = window.finish(WAIT);
        String json = new ObjectMapper().findAndRegisterModules().writeValueAsString(result);
        assertThat(json)
                .doesNotContain("private-sql-body", "private-bind-value", READ, "SELECT id")
                .contains("templateSha256", "borrows", "elapsed");
        assertThat(result.executions().getFirst().templateSha256()).matches("[0-9a-f]{64}");
        assertThatThrownBy(() -> result.families().put("injected", 1L))
                .isInstanceOf(UnsupportedOperationException.class);
        assertThatThrownBy(() -> result.executions().clear())
                .isInstanceOf(UnsupportedOperationException.class);
        assertThat(window.awaitSqlFamily("absent", 1, Duration.ZERO)).isFalse();
    }

    @Test
    void classifiesWorkContextAndFrozenMemoryReadsAndWritesByTheirRealTables() throws Exception {
        var recorder = new ApplicationJdbcRecorder();
        var database = new Database();
        var source = recorder.wrap(database.source);
        var window = recorder.begin("context-memory-families");
        try (var connection = source.getConnection()) {
            for (String sql :
                    List.of(
                            "SELECT w.payload::text,w.transcript::text FROM agent_runtime.work_contexts w"
                                    + " JOIN business.turns t ON t.id=w.turn_id WHERE w.turn_id=?",
                            "INSERT INTO agent_runtime.work_contexts (turn_id,payload,transcript)"
                                    + " VALUES(?,?::jsonb,?::jsonb) ON CONFLICT(turn_id) DO UPDATE"
                                    + " SET transcript=EXCLUDED.transcript",
                            "SELECT epoch,revision,markdown FROM memory.user_profiles WHERE tenant=? AND subject=?",
                            "INSERT INTO memory.user_profiles(tenant,subject,epoch,revision,markdown) VALUES(?,?,?,?,?)",
                            "SELECT p.context::text FROM memory.turn_snapshots p JOIN memory.sources s ON s.id=p.turn_id WHERE p.turn_id=?",
                            "INSERT INTO memory.turn_snapshots(turn_id,tenant,subject,epoch,context) VALUES(?,?,?,?,?)")) {
                connection.prepareStatement(sql).executeUpdate();
            }
        }
        var result = window.finish(WAIT);
        assertThat(result.families())
                .containsOnly(
                        entry("work_context_read", 1L),
                        entry("work_context_insert", 1L),
                        entry("profile_read", 1L),
                        entry("profile_insert", 1L),
                        entry("turn_snapshot_read", 1L),
                        entry("turn_snapshot_insert", 1L));
    }

    private static void await(CountDownLatch latch) {
        try {
            if (!latch.await(10, TimeUnit.SECONDS))
                throw new AssertionError("Fixture latch timeout");
        } catch (InterruptedException interrupted) {
            Thread.currentThread().interrupt();
            throw new AssertionError("Fixture interrupted", interrupted);
        }
    }

    @FunctionalInterface
    private interface ConnectionFactory {
        Connection get() throws SQLException;
    }

    private static final class StubDataSource extends AbstractDataSource implements AutoCloseable {
        private final ConnectionFactory factory;
        private final AtomicInteger closes = new AtomicInteger();

        private StubDataSource(ConnectionFactory factory) {
            this.factory = factory;
        }

        @Override
        public Connection getConnection() throws SQLException {
            return factory.get();
        }

        @Override
        public Connection getConnection(String username, String password) throws SQLException {
            return getConnection();
        }

        @Override
        public void close() {
            closes.incrementAndGet();
        }
    }

    private static final class Database {
        private final List<Connection> connections = new CopyOnWriteArrayList<>();
        private final StubDataSource source = new StubDataSource(this::connection);

        private Connection connection() throws SQLException {
            var connection = mock(Connection.class);
            when(connection.prepareStatement(anyString()))
                    .thenAnswer(
                            ignored -> {
                                var statement = mock(PreparedStatement.class);
                                when(statement.executeUpdate()).thenReturn(1);
                                return statement;
                            });
            var direct = mock(Statement.class);
            when(direct.executeUpdate(anyString())).thenReturn(1);
            when(direct.executeBatch()).thenReturn(new int[] {1, 1});
            when(connection.createStatement()).thenReturn(direct);
            connections.add(connection);
            return connection;
        }
    }
}
