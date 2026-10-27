package com.financeclaw.bootstrap;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.Mockito.*;

import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.Statement;
import javax.sql.DataSource;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.springframework.boot.autoconfigure.AutoConfigurations;
import org.springframework.boot.autoconfigure.jdbc.JdbcTemplateAutoConfiguration;
import org.springframework.boot.test.context.runner.ApplicationContextRunner;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.support.JdbcTransactionManager;
import org.springframework.mock.env.MockEnvironment;
import org.springframework.transaction.support.TransactionTemplate;

/** 真实 Boot 模板配置必须传到运行 SQL；JDBC 记录桩不替代 PostgreSQL 业务验收。 */
class RuntimeJdbcConfigurationTest {
    @ParameterizedTest
    @ValueSource(booleans = {false, true})
    void runtimeQueriesUseConfiguredStatementSettingsAndTheCallerTransaction(boolean transactional)
            throws Exception {
        var source = mock(DataSource.class);
        var connection = mock(Connection.class);
        var workers = mock(PreparedStatement.class);
        var artifacts = mock(PreparedStatement.class);
        var usage = mock(Statement.class);
        var empty = mock(ResultSet.class);
        var counts = mock(ResultSet.class);
        when(source.getConnection()).thenReturn(connection);
        when(connection.getAutoCommit()).thenReturn(true);
        when(connection.prepareStatement(anyString())).thenReturn(workers, artifacts);
        when(connection.createStatement()).thenReturn(usage);
        when(workers.executeQuery()).thenReturn(empty);
        when(artifacts.executeQuery()).thenReturn(empty);
        when(usage.executeQuery(anyString())).thenReturn(counts);
        when(counts.next()).thenReturn(true, false);

        new ApplicationContextRunner()
                .withConfiguration(AutoConfigurations.of(JdbcTemplateAutoConfiguration.class))
                .withBean(DataSource.class, () -> source)
                .withPropertyValues(
                        "spring.jdbc.template.query-timeout=3s",
                        "spring.jdbc.template.fetch-size=17",
                        "spring.jdbc.template.max-rows=29")
                .run(
                        context -> {
                            assertThat(context).hasNotFailed();
                            var sql = context.getBean(JdbcTemplate.class);
                            var assembly = new RuntimeCoordinationConfiguration();
                            var environment = new MockEnvironment();
                            var registry = assembly.workerRegistry(sql, environment);
                            var catalog = assembly.runtimeArtifactCatalog(sql, environment);
                            var totals = assembly.runtimeUsage(sql, environment);
                            Runnable query =
                                    () -> {
                                        assertThat(registry.list(5)).isEmpty();
                                        assertThat(catalog.list(5)).isEmpty();
                                        assertThat(totals.snapshot().modelCalls()).isZero();
                                    };
                            if (transactional) {
                                new TransactionTemplate(new JdbcTransactionManager(source))
                                        .executeWithoutResult(status -> query.run());
                            } else {
                                query.run();
                            }

                            for (Statement statement :
                                    new Statement[] {workers, artifacts, usage}) {
                                verify(statement).setQueryTimeout(3);
                                verify(statement).setFetchSize(17);
                                verify(statement).setMaxRows(29);
                                verify(statement).close();
                            }
                            // 同一事务中的三条查询复用一条线程绑定连接；事务外按调用归还。
                            verify(source, times(transactional ? 1 : 3)).getConnection();
                            verify(connection, times(transactional ? 1 : 3)).close();
                            if (transactional) {
                                verify(connection).commit();
                                verify(connection, never()).rollback();
                            }
                        });
    }
}
