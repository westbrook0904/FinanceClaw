package com.financeclaw.infrastructure.memory;

import com.financeclaw.app.memory.port.ForgetLedger;
import com.financeclaw.domain.memory.ForgetFact;
import com.zaxxer.hikari.HikariDataSource;
import java.util.List;
import org.springframework.context.annotation.*;
import org.springframework.core.env.Environment;

/**
 * 装配独立遗忘账本及其专用 PostgreSQL 连接池。 账本连接不注册为业务 DataSource，防止 MyBatis 或 Flyway
 * 意外使用独立权威库；超时与小连接池限制故障影响。未启用时提供显式不可用的账本实现，不伪造追加成功。
 */
@Configuration(proxyBeanMethods = false)
public class ForgetLedgerConfiguration {
    @Bean(destroyMethod = "close")
    ForgetLedger forgetLedger(Environment env) {
        String prefix = "financeclaw.memory.forget-ledger.";
        if (!env.getProperty(prefix + "enabled", Boolean.class, false)) {
            return new Disabled();
        }
        var pool = new HikariDataSource();
        String url = env.getRequiredProperty(prefix + "url");
        if (!url.startsWith("jdbc:postgresql:")) {
            throw new IllegalStateException("forget_ledger_requires_postgresql");
        }
        pool.setJdbcUrl(url);
        pool.setUsername(env.getRequiredProperty(prefix + "username"));
        pool.setPassword(env.getRequiredProperty(prefix + "password"));
        pool.setPoolName("privacy-ledger");
        pool.setMaximumPoolSize(2);
        pool.setMinimumIdle(0);
        pool.setConnectionTimeout(5000);
        pool.setInitializationFailTimeout(-1);
        pool.addDataSourceProperty("connectTimeout", "5");
        pool.addDataSourceProperty("socketTimeout", "10");
        pool.addDataSourceProperty("options", "-c statement_timeout=5000 -c lock_timeout=3000");
        // Not a Spring DataSource bean: business MyBatis/Flyway keep their original primary pool.
        return new PostgresForgetLedger(pool, true);
    }

    private static final class Disabled implements ForgetLedger {
        @Override
        public boolean enabled() {
            return false;
        }

        @Override
        public void initialize(String lineage, List<ForgetFact> history) {
            unavailable();
        }

        @Override
        public void append(ForgetFact fact) {
            unavailable();
        }

        @Override
        public List<ForgetFact> snapshot(String lineage) {
            throw unavailable();
        }

        private IllegalStateException unavailable() {
            throw new IllegalStateException("forget_ledger_disabled");
        }
    }
}
