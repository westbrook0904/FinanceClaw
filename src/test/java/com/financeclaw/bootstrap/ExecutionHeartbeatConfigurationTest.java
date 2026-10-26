package com.financeclaw.bootstrap;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.Mockito.*;

import com.financeclaw.infrastructure.task.HeartbeatResources;
import com.zaxxer.hikari.HikariDataSource;
import com.zaxxer.hikari.HikariPoolMXBean;
import io.micrometer.core.instrument.simple.SimpleMeterRegistry;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.springframework.jdbc.datasource.DelegatingDataSource;
import org.springframework.mock.env.MockEnvironment;

class ExecutionHeartbeatConfigurationTest {
    private static HikariDataSource source() {
        var source = new HikariDataSource();
        source.setJdbcUrl("jdbc:postgresql://localhost:5432/synthetic");
        source.setUsername("synthetic");
        source.setPassword("synthetic");
        source.setMaximumPoolSize(8);
        source.setMinimumIdle(8);
        source.addDataSourceProperty("options", "-c statement_timeout=5000 -c lock_timeout=3000");
        return source;
    }

    @Test
    void totalBudgetIsReservedBeforePoolStartAndWrapperIdentityIsPreserved() {
        try (var source = source()) {
            var wrapped = new DelegatingDataSource(source);
            var reservation = new ExecutionHeartbeatConfiguration.Reservation(2);
            assertThat(reservation.postProcessAfterInitialization(wrapped, "dataSource"))
                    .isSameAs(wrapped);
            assertThat(source.getMaximumPoolSize()).isEqualTo(6);
            assertThat(source.getMinimumIdle()).isEqualTo(6);
            assertThat(source.getHikariPoolMXBean()).isNull();
        }
    }

    @Test
    void startedPoolCannotBeResizedAndUnrelatedBeansAreUntouched() throws Exception {
        var source = mock(HikariDataSource.class);
        when(source.unwrap(HikariDataSource.class)).thenReturn(source);
        when(source.getHikariPoolMXBean()).thenReturn(mock(HikariPoolMXBean.class));
        var reservation = new ExecutionHeartbeatConfiguration.Reservation(2);
        assertThatThrownBy(() -> reservation.postProcessAfterInitialization(source, "dataSource"))
                .isInstanceOf(IllegalStateException.class)
                .hasMessage("heartbeat_pool_reservation_requires_unstarted_pool");
        verify(source, never()).setMaximumPoolSize(anyInt());
        var unrelated = new Object();
        assertThat(reservation.postProcessAfterInitialization(unrelated, "other"))
                .isSameAs(unrelated);
    }

    @Test
    void totalBudgetMustLeaveBusinessCapacity() {
        try (var source = source()) {
            source.setMaximumPoolSize(2);
            assertThatThrownBy(
                            () ->
                                    new ExecutionHeartbeatConfiguration.Reservation(2)
                                            .postProcessAfterInitialization(source, "dataSource"))
                    .isInstanceOf(IllegalArgumentException.class);
            assertThat(source.getMaximumPoolSize()).isEqualTo(2);
        }
    }

    @Test
    void privatePoolCopiesCredentialsAndPropertiesWithoutMutatingMainOrStartingEitherPool() {
        var metrics = new SimpleMeterRegistry();
        try (var main = source()) {
            main.setCatalog("catalog");
            main.setSchema("public");
            main.setTransactionIsolation("TRANSACTION_REPEATABLE_READ");
            main.setAutoCommit(false);
            main.setIsolateInternalQueries(false);
            main.setConnectionTestQuery("SELECT 1");
            main.setConnectionInitSql("SET application_name = 'synthetic-parent'");
            main.addDataSourceProperty("sslmode", "verify-full");
            var settings = ExecutionHeartbeatConfiguration.Settings.read(new MockEnvironment());
            new ExecutionHeartbeatConfiguration.Reservation(settings.reserved())
                    .postProcessAfterInitialization(main, "dataSource");
            try (var heartbeat =
                    ExecutionHeartbeatConfiguration.privatePool(main, settings, metrics)) {
                assertThat(main.getMaximumPoolSize() + heartbeat.getMaximumPoolSize()).isEqualTo(8);
                assertThat(heartbeat.getMaximumPoolSize()).isEqualTo(2);
                assertThat(heartbeat.getMinimumIdle()).isZero();
                assertThat(heartbeat.getJdbcUrl()).startsWith(main.getJdbcUrl() + "?");
                assertThat(heartbeat.getUsername()).isEqualTo(main.getUsername());
                assertThat(heartbeat.getPassword()).isEqualTo(main.getPassword());
                assertThat(heartbeat.getDataSourceProperties())
                        .isNotSameAs(main.getDataSourceProperties());
                heartbeat.addDataSourceProperty("synthetic", "private");
                assertThat(main.getDataSourceProperties()).doesNotContainKey("synthetic");
                assertThat(main.getDataSourceProperties().getProperty("options"))
                        .isEqualTo("-c statement_timeout=5000 -c lock_timeout=3000");
                assertThat(heartbeat.getCatalog()).isEqualTo(main.getCatalog());
                assertThat(heartbeat.getSchema()).isEqualTo(main.getSchema());
                assertThat(heartbeat.getTransactionIsolation())
                        .isEqualTo(main.getTransactionIsolation());
                assertThat(heartbeat.isAutoCommit()).isEqualTo(main.isAutoCommit());
                assertThat(heartbeat.isIsolateInternalQueries()).isTrue();
                assertThat(main.isIsolateInternalQueries()).isFalse();
                assertThat(heartbeat.isReadOnly()).isEqualTo(main.isReadOnly());
                assertThat(heartbeat.getConnectionTestQuery())
                        .isEqualTo(main.getConnectionTestQuery());
                assertThat(heartbeat.getMaxLifetime()).isEqualTo(main.getMaxLifetime());
                assertThat(heartbeat.getIdleTimeout()).isEqualTo(main.getIdleTimeout());
                assertThat(heartbeat.getKeepaliveTime()).isEqualTo(main.getKeepaliveTime());
                assertThat(heartbeat.getDataSourceProperties().getProperty("sslmode"))
                        .isEqualTo("verify-full");
                assertThat(heartbeat.getDataSourceProperties().getProperty("socketTimeout"))
                        .isEqualTo("2");
                assertThat(main.getDataSourceProperties()).doesNotContainKey("socketTimeout");
                assertThat(heartbeat.getConnectionInitSql())
                        .isEqualTo(
                                "SET application_name = 'synthetic-parent'\n; SET lock_timeout = '750ms'; SET statement_timeout = '1000ms'");
                assertThat(main.getConnectionInitSql())
                        .isEqualTo("SET application_name = 'synthetic-parent'");
                assertThat(main.getHikariPoolMXBean()).isNull();
                assertThat(heartbeat.getHikariPoolMXBean()).isNull();
            }
        } finally {
            metrics.close();
        }
    }

    @Test
    void urlTimeoutsCannotOverrideTheDedicatedNetworkBoundsOrChangeOtherConnectionParameters() {
        try (var main = source()) {
            String original =
                    "jdbc:postgresql://localhost:5432/synthetic?sslmode=verify-full"
                            + "&options=-c%20statement_timeout%3D0&socketTimeout=0&connectTimeout=999"
                            + "&loginTimeout=0&cancelSignalTimeout=0&socketTimeout=999";
            main.setJdbcUrl(original);
            main.setConnectionInitSql("SET application_name = 'synthetic' -- connection label");
            main.addDataSourceProperty("socketTimeout", "999");
            var settings = ExecutionHeartbeatConfiguration.Settings.read(new MockEnvironment());
            try (var heartbeat =
                    ExecutionHeartbeatConfiguration.privatePool(main, settings, null)) {
                var connectionProperties = new java.util.Properties();
                connectionProperties.putAll(heartbeat.getDataSourceProperties());
                connectionProperties.setProperty("user", heartbeat.getUsername());
                connectionProperties.setProperty("password", heartbeat.getPassword());
                var parsed =
                        org.postgresql.Driver.parseURL(
                                heartbeat.getJdbcUrl(), connectionProperties);
                assertThat(parsed).isNotNull();
                for (String property :
                        java.util.List.of(
                                "socketTimeout",
                                "connectTimeout",
                                "loginTimeout",
                                "cancelSignalTimeout")) {
                    assertThat(parsed.getProperty(property)).isEqualTo("2");
                }
                assertThat(parsed.getProperty("sslmode")).isEqualTo("verify-full");
                assertThat(parsed.getProperty("options")).isEqualTo("-c statement_timeout=0");
                assertThat(heartbeat.getConnectionInitSql())
                        .contains("-- connection label\n; SET lock_timeout");
                assertThat(main.getJdbcUrl()).isEqualTo(original);
                assertThat(main.getDataSourceProperties().getProperty("socketTimeout"))
                        .isEqualTo("999");
            }
        }
    }

    @Test
    void unsupportedDataSourceClassModeIsRejectedInsteadOfPartiallyCopied() {
        try (var main = source()) {
            main.setDataSourceClassName("org.postgresql.ds.PGSimpleDataSource");
            assertThatThrownBy(
                            () ->
                                    ExecutionHeartbeatConfiguration.privatePool(
                                            main,
                                            ExecutionHeartbeatConfiguration.Settings.read(
                                                    new MockEnvironment()),
                                            null))
                    .isInstanceOf(IllegalStateException.class)
                    .hasMessage("heartbeat_requires_postgresql_hikari_url");
        }
    }

    @ParameterizedTest
    @ValueSource(longs = {1500, 2000, 15000, 60000})
    void defaultsDeriveBoundedTimeoutsForExistingShortAndNormalLeaseConfigurations(long lease) {
        var env =
                new MockEnvironment()
                        .withProperty("financeclaw.worker.lease-ms", Long.toString(lease))
                        .withProperty("financeclaw.worker.heartbeat-ms", "100");
        var settings = ExecutionHeartbeatConfiguration.Settings.read(env);
        assertThat(settings.borrowMillis()).isBetween(250L, 1000L);
        assertThat(settings.statementMillis()).isBetween(50L, 1000L);
        assertThat(settings.lockMillis()).isLessThanOrEqualTo(settings.statementMillis());
        assertThat(settings.borrowMillis() + settings.statementMillis()).isLessThan(lease - 100);
    }

    @Test
    void explicitUnboundedOrInfeasibleTimeoutsAreRejected() {
        for (var setting :
                Map.of(
                                "connection-timeout-ms",
                                "249",
                                "statement-timeout-ms",
                                "6000",
                                "reserved-connections",
                                "0",
                                "lock-timeout-ms",
                                "2000")
                        .entrySet()) {
            var env =
                    new MockEnvironment()
                            .withProperty(
                                    "financeclaw.worker.heartbeat-pool." + setting.getKey(),
                                    setting.getValue());
            assertThatThrownBy(() -> ExecutionHeartbeatConfiguration.Settings.read(env))
                    .isInstanceOf(IllegalArgumentException.class);
        }
        var overflowingTick =
                new MockEnvironment()
                        .withProperty("financeclaw.worker.lease-ms", Long.toString(Long.MAX_VALUE))
                        .withProperty(
                                "financeclaw.worker.heartbeat-ms",
                                Long.toString(Long.MAX_VALUE / 2));
        assertThatThrownBy(() -> ExecutionHeartbeatConfiguration.Settings.read(overflowingTick))
                .isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void restrictedAndOfflineEntrypointsDoNotImportHeartbeatResources() {
        for (Class<?> entrypoint :
                java.util.List.of(
                        com.financeclawmemory.MemoryWorkerApplication.class,
                        com.financeclawsetup.DatabaseInitializationApplication.class,
                        com.financeclawprivacy.PrivacyRecoveryApplication.class,
                        com.financeclawrelease.ReleaseAdministrationApplication.class,
                        com.financeclawagent.AgentAdministrationApplication.class)) {
            var visited = new java.util.HashSet<Class<?>>();
            collectImports(entrypoint, visited);
            assertThat(visited)
                    .doesNotContain(
                            ExecutionHeartbeatConfiguration.class, HeartbeatResources.class);
        }
    }

    private static void collectImports(Class<?> type, java.util.Set<Class<?>> visited) {
        if (!visited.add(type)) return;
        var imports = type.getAnnotation(org.springframework.context.annotation.Import.class);
        if (imports != null)
            for (Class<?> imported : imports.value()) collectImports(imported, visited);
    }

    @Test
    void decoratedResourcesShareOneCloseOwner() {
        var pool = mock(HikariDataSource.class);
        var resources = new HeartbeatResources(pool);
        var wrapped = new DelegatingDataSource(pool);
        var calls = new AtomicInteger();
        var observed =
                resources.decorate(
                        source -> {
                            calls.incrementAndGet();
                            return wrapped;
                        });
        assertThat(observed.dataSource()).isSameAs(wrapped);
        assertThat(calls).hasValue(1);
        observed.close();
        resources.close();
        verify(pool, times(1)).close();
    }
}
