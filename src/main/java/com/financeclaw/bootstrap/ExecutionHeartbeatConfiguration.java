package com.financeclaw.bootstrap;

import com.financeclaw.app.execution.port.HeartbeatGateway;
import com.financeclaw.infrastructure.task.HeartbeatResources;
import com.financeclaw.infrastructure.task.MyBatisHeartbeatGateway;
import com.zaxxer.hikari.HikariDataSource;
import com.zaxxer.hikari.metrics.micrometer.MicrometerMetricsTrackerFactory;
import io.micrometer.core.instrument.MeterRegistry;
import java.sql.SQLException;
import java.util.ArrayList;
import java.util.Properties;
import java.util.Set;
import javax.sql.DataSource;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.beans.factory.config.BeanPostProcessor;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.core.Ordered;
import org.springframework.core.PriorityOrdered;
import org.springframework.core.env.Environment;

/**
 * 从原主池预算中预留续租连接，隔离多用户业务连接占满造成的续租等待。
 *
 * <p>原 spring.datasource.hikari.maximum-pool-size 是本进程两个同库池的总上限； 默认 8 = 业务 6 + 续租 2。只在
 * GovernedTasks 装配，不影响受限 Memory/管理 CLI。 主池仍是 Boot 唯一 DataSource Bean，原事务和全局 MyBatis 装配不变。
 */
@Configuration(proxyBeanMethods = false)
public class ExecutionHeartbeatConfiguration {
    private static final String PREFIX = "financeclaw.worker.heartbeat-pool.";
    private static final Set<String> NETWORK_TIMEOUTS =
            Set.of("connectTimeout", "socketTimeout", "loginTimeout", "cancelSignalTimeout");

    /**
     * 配置绑定完成、消费者获得 Bean 前扣除预留量；禁止启动后缩池，因为那会暂时超过总连接预算。 观察器若已包装 DataSource，可用标准 unwrap 找到原
     * Hikari，不要求把代理资源换回裸池。
     */
    @Bean
    static BeanPostProcessor heartbeatPoolReservation(Environment env) {
        return new Reservation(Settings.read(env).reserved());
    }

    @Bean(destroyMethod = "close")
    HeartbeatResources heartbeatResources(
            DataSource business, Environment env, ObjectProvider<MeterRegistry> registry)
            throws SQLException {
        var settings = Settings.read(env);
        var main = business.unwrap(HikariDataSource.class);
        var pool = privatePool(main, settings, registry.getIfAvailable());
        return new HeartbeatResources(pool);
    }

    @Bean
    HeartbeatGateway heartbeatGateway(
            HeartbeatResources resources, Environment env, ObjectProvider<MeterRegistry> registry)
            throws Exception {
        var settings = Settings.read(env);
        return new MyBatisHeartbeatGateway(
                resources,
                settings.statementSeconds(),
                settings.transactionSeconds(),
                registry.getIfAvailable());
    }

    static HikariDataSource privatePool(
            HikariDataSource main, Settings settings, MeterRegistry metrics) {
        if (main.getJdbcUrl() == null
                || !main.getJdbcUrl().startsWith("jdbc:postgresql:")
                || main.getDataSource() != null
                || main.getDataSourceJNDI() != null
                || main.getDataSourceClassName() != null) {
            throw new IllegalStateException("heartbeat_requires_postgresql_hikari_url");
        }
        var pool = new HikariDataSource();
        copyConnectionConfiguration(main, pool);
        // autoCommit=false 时让 Hikari 独立提交 connectionInitSql，防止首次业务回滚撤销 SET 超时。
        // 保留主池的自动提交行为；此开关只改变私池初始化/健康检查事务的隔离。
        pool.setIsolateInternalQueries(true);
        pool.setPoolName("execution-heartbeat");
        pool.setMaximumPoolSize(settings.reserved());
        pool.setMinimumIdle(0);
        pool.setConnectionTimeout(settings.borrowMillis());
        pool.setValidationTimeout(Math.max(250, Math.min(500, settings.borrowMillis())));
        pool.setInitializationFailTimeout(-1);
        pool.setRegisterMbeans(false);
        pool.setAllowPoolSuspension(false);
        pool.setScheduledExecutor(null);
        pool.setThreadFactory(null);
        pool.setMetricsTrackerFactory(
                metrics == null ? null : new MicrometerMetricsTrackerFactory(metrics));
        for (String property : NETWORK_TIMEOUTS) {
            pool.addDataSourceProperty(property, Integer.toString(settings.networkSeconds()));
        }
        // pgjdbc 的 URL 参数优先于 Properties；只替换网络超时，保留其他原始编码和连接参数。
        pool.setJdbcUrl(boundedNetworkUrl(main.getJdbcUrl(), settings.networkSeconds()));
        // URL 的 options 可能优先于 driver Properties，session 初始化明确建立毫秒级锁/语句上限。
        // 保留原初始化动作；它们每个物理连接仅执行一次，不为每个 heartbeat 添加 SET 查询。
        String previous = main.getConnectionInitSql();
        String prefix = previous == null || previous.isBlank() ? "" : previous + "\n; ";
        pool.setConnectionInitSql(
                prefix
                        + "SET lock_timeout = '"
                        + settings.lockMillis()
                        + "ms'; SET statement_timeout = '"
                        + settings.statementMillis()
                        + "ms'");
        return pool;
    }

    private static String boundedNetworkUrl(String url, int seconds) {
        int query = url.indexOf('?');
        String base = query < 0 ? url : url.substring(0, query);
        var parameters = new ArrayList<String>();
        if (query >= 0) {
            for (String parameter : url.substring(query + 1).split("&")) {
                int equals = parameter.indexOf('=');
                String key = equals < 0 ? parameter : parameter.substring(0, equals);
                if (!parameter.isEmpty() && !NETWORK_TIMEOUTS.contains(key))
                    parameters.add(parameter);
            }
        }
        for (String property : NETWORK_TIMEOUTS.stream().sorted().toList()) {
            parameters.add(property + "=" + seconds);
        }
        return base + "?" + String.join("&", parameters);
    }

    /**
     * 只用公开 getter 复制 URL 模式的连接行为。不要调用 copyStateTo：它会共享源池 Properties， 且 setDataSourceProperties 是
     * putAll 而不是替换，后续覆写私池超时会污染主池。 新 HikariDataSource 自己持有独立 Properties；这里只把键值拷入它的空集合。
     *
     * <p>保留凭据、URL/SSL 驱动属性、schema/catalog/隔离级别、只读/自动提交、连接测试和连接寿命行为。 外部
     * DataSource/JNDI/dataSourceClassName 模式上方明确拒绝；主池的指标对象、线程/调度器、 MBean
     * 注册和池大小不复制，避免共享生命周期或把主池策略带入专用池。
     */
    static void copyConnectionConfiguration(HikariDataSource main, HikariDataSource pool) {
        pool.setJdbcUrl(main.getJdbcUrl());
        if (main.getDriverClassName() != null) pool.setDriverClassName(main.getDriverClassName());
        pool.setUsername(main.getUsername());
        pool.setPassword(main.getPassword());
        pool.setCatalog(main.getCatalog());
        pool.setSchema(main.getSchema());
        pool.setTransactionIsolation(main.getTransactionIsolation());
        pool.setAutoCommit(main.isAutoCommit());
        pool.setReadOnly(main.isReadOnly());
        pool.setConnectionTestQuery(main.getConnectionTestQuery());
        pool.setConnectionInitSql(main.getConnectionInitSql());
        pool.setIdleTimeout(main.getIdleTimeout());
        pool.setMaxLifetime(main.getMaxLifetime());
        pool.setKeepaliveTime(main.getKeepaliveTime());
        pool.setLeakDetectionThreshold(main.getLeakDetectionThreshold());
        pool.setIsolateInternalQueries(main.isIsolateInternalQueries());
        if (main.getExceptionOverrideClassName() != null)
            pool.setExceptionOverrideClassName(main.getExceptionOverrideClassName());
        else if (main.getExceptionOverride() != null)
            pool.setExceptionOverride(main.getExceptionOverride());
        var properties = new Properties();
        properties.putAll(main.getDataSourceProperties());
        pool.setDataSourceProperties(properties);
    }

    /** 有界默认值随短租约收缩；Hikari 获取连接最小 250ms，无法满足时明确拒绝配置。 */
    record Settings(
            int reserved,
            long borrowMillis,
            long statementMillis,
            long lockMillis,
            long leaseMillis,
            long heartbeatMillis) {
        Settings {
            if (reserved < 1
                    || reserved > 32
                    || borrowMillis < 250
                    || borrowMillis > 5000
                    || statementMillis < 50
                    || statementMillis > 5000
                    || lockMillis < 1
                    || lockMillis > statementMillis
                    || heartbeatMillis < 100
                    || leaseMillis <= 0
                    || heartbeatMillis > leaseMillis / 3
                    || borrowMillis + statementMillis >= leaseMillis - heartbeatMillis) {
                throw new IllegalArgumentException("invalid_heartbeat_pool_limits");
            }
        }

        static Settings read(Environment env) {
            long lease = env.getProperty("financeclaw.worker.lease-ms", Long.class, 15000L);
            long tick = env.getProperty("financeclaw.worker.heartbeat-ms", Long.class, 2000L);
            long statement =
                    configured(
                            env, "statement-timeout-ms", Math.max(50, Math.min(1000, lease / 6)));
            return new Settings(
                    env.getProperty(PREFIX + "reserved-connections", Integer.class, 2),
                    configured(
                            env, "connection-timeout-ms", Math.max(250, Math.min(1000, lease / 6))),
                    statement,
                    configured(
                            env, "lock-timeout-ms", Math.max(1, Math.min(750, statement * 3 / 4))),
                    lease,
                    tick);
        }

        private static long configured(Environment env, String property, long derived) {
            long value = env.getProperty(PREFIX + property, Long.class, 0L);
            return value == 0 ? derived : value;
        }

        int statementSeconds() {
            return (int) Math.max(1, (statementMillis + 999) / 1000);
        }

        int transactionSeconds() {
            return (int) Math.max(1, (borrowMillis + statementMillis + 999) / 1000);
        }

        int networkSeconds() {
            return (int) Math.max(1, (statementMillis + 999) / 1000 + 1);
        }
    }

    static final class Reservation implements BeanPostProcessor, PriorityOrdered {
        private final int reserved;

        Reservation(int reserved) {
            this.reserved = reserved;
        }

        @Override
        public int getOrder() {
            return Ordered.HIGHEST_PRECEDENCE;
        }

        @Override
        public Object postProcessAfterInitialization(Object bean, String name) {
            if (!name.equals("dataSource") || !(bean instanceof DataSource source)) return bean;
            try {
                var pool = source.unwrap(HikariDataSource.class);
                if (pool.getHikariPoolMXBean() != null || pool.isClosed()) {
                    throw new IllegalStateException(
                            "heartbeat_pool_reservation_requires_unstarted_pool");
                }
                int maximum = pool.getMaximumPoolSize();
                if (maximum <= reserved) {
                    throw new IllegalArgumentException(
                            "heartbeat_pool_budget_requires_business_capacity");
                }
                int business = maximum - reserved;
                int minimum = pool.getMinimumIdle();
                pool.setMaximumPoolSize(business);
                pool.setMinimumIdle(minimum < 0 ? business : Math.min(minimum, business));
                return bean;
            } catch (SQLException failure) {
                throw new IllegalStateException("heartbeat_requires_hikari_data_source", failure);
            }
        }
    }
}
