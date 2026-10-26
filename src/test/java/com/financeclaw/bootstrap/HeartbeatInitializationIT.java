package com.financeclaw.bootstrap;

import static org.assertj.core.api.Assertions.*;

import com.financeclaw.domain.Task.*;
import com.financeclaw.infrastructure.task.HeartbeatResources;
import com.financeclaw.infrastructure.task.MyBatisHeartbeatGateway;
import com.zaxxer.hikari.HikariDataSource;
import java.sql.Connection;
import java.sql.SQLException;
import java.util.concurrent.atomic.AtomicInteger;
import org.flywaydb.core.Flyway;
import org.junit.jupiter.api.Test;
import org.postgresql.PGConnection;
import org.springframework.jdbc.datasource.DelegatingDataSource;
import org.springframework.jdbc.datasource.DriverManagerDataSource;
import org.springframework.mock.env.MockEnvironment;
import org.testcontainers.containers.PostgreSQLContainer;

/** 真 PG 验证非自动提交连接的初始化 SET 不属于首次心跳事务。 */
class HeartbeatInitializationIT {
    @Test
    void firstFencedHeartbeatRollbackPreservesBothSessionTimeoutsOnTheSamePhysicalConnection()
            throws Exception {
        try (var postgres = new PostgreSQLContainer<>("postgres:16.10-alpine")) {
            postgres.start();
            var setup =
                    new DriverManagerDataSource(
                            postgres.getJdbcUrl(), postgres.getUsername(), postgres.getPassword());
            Flyway.configure().dataSource(setup).load().migrate();
            try (var main = new HikariDataSource()) {
                main.setJdbcUrl(postgres.getJdbcUrl());
                main.setUsername(postgres.getUsername());
                main.setPassword(postgres.getPassword());
                main.setAutoCommit(false);
                main.setIsolateInternalQueries(false);
                main.addDataSourceProperty("options", "-c statement_timeout=0 -c lock_timeout=0");
                var settings =
                        ExecutionHeartbeatConfiguration.Settings.read(
                                new MockEnvironment()
                                        .withProperty(
                                                "financeclaw.worker.heartbeat-pool.reserved-connections",
                                                "1"));
                var pool = ExecutionHeartbeatConfiguration.privatePool(main, settings, null);
                var firstBackend = new AtomicInteger();
                try (var resources =
                        new HeartbeatResources(pool)
                                .decorate(
                                        source ->
                                                new DelegatingDataSource(source) {
                                                    @Override
                                                    public Connection getConnection()
                                                            throws SQLException {
                                                        var connection = super.getConnection();
                                                        firstBackend.compareAndSet(
                                                                0,
                                                                connection
                                                                        .unwrap(PGConnection.class)
                                                                        .getBackendPID());
                                                        return connection;
                                                    }
                                                })) {
                    var gateway =
                            new MyBatisHeartbeatGateway(
                                    resources,
                                    settings.statementSeconds(),
                                    settings.transactionSeconds(),
                                    null);
                    assertThat(pool.getHikariPoolMXBean()).isNull();
                    var missing =
                            new Claim(
                                    new Command(
                                            "missing",
                                            "missing-turn",
                                            "running",
                                            "attempt",
                                            1,
                                            true,
                                            null),
                                    new Turn(
                                            "missing-turn",
                                            "conversation",
                                            "tenant",
                                            "subject",
                                            "key",
                                            "hash",
                                            "question",
                                            "release",
                                            "running",
                                            1,
                                            null,
                                            null));
                    assertThatThrownBy(() -> gateway.renew(missing, 15000))
                            .isInstanceOf(Fenced.class);
                    assertThat(pool.getHikariPoolMXBean().getActiveConnections()).isZero();
                    assertThat(firstBackend.get()).isPositive();
                    try (var connection = pool.getConnection()) {
                        assertThat(connection.unwrap(PGConnection.class).getBackendPID())
                                .isEqualTo(firstBackend.get());
                        assertThat(connection.getAutoCommit()).isFalse();
                        assertThat(show(connection, "SHOW lock_timeout")).isEqualTo("750ms");
                        assertThat(show(connection, "SHOW statement_timeout")).isEqualTo("1s");
                        connection.rollback();
                    }
                    assertThat(main.isAutoCommit()).isFalse();
                    assertThat(main.isIsolateInternalQueries()).isFalse();
                }
                assertThat(pool.isClosed()).isTrue();
            }
        }
    }

    private static String show(Connection connection, String sql) throws SQLException {
        try (var statement = connection.createStatement();
                var rows = statement.executeQuery(sql)) {
            assertThat(rows.next()).isTrue();
            return rows.getString(1);
        }
    }
}
