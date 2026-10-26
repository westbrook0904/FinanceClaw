package com.financeclaw.infrastructure.task;

import com.zaxxer.hikari.HikariDataSource;
import java.util.Objects;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.function.UnaryOperator;
import javax.sql.DataSource;

/**
 * 专用续租池的所有权。该对象不是 DataSource、事务管理器或 SqlSessionFactory Bean， 因而不会改变 Boot 的业务库自动装配候选或受限进程的数据库角色。
 *
 * <p>decorate 在消费者初始化前返回共享同一所有权的不可变资源视图，允许监控或测试包装逻辑连接； 私有事务管理器与 MyBatis 必须同时使用 dataSource()
 * 返回的同一对象。关闭只转交一次真实池。
 */
public final class HeartbeatResources implements AutoCloseable {
    private final HikariDataSource pool;
    private final DataSource source;
    private final AtomicBoolean closed;

    public HeartbeatResources(HikariDataSource pool) {
        this(pool, pool, new AtomicBoolean());
    }

    private HeartbeatResources(HikariDataSource pool, DataSource source, AtomicBoolean closed) {
        this.pool = Objects.requireNonNull(pool);
        this.source = Objects.requireNonNull(source);
        this.closed = closed;
    }

    public DataSource dataSource() {
        return source;
    }

    public HeartbeatResources decorate(UnaryOperator<DataSource> wrapper) {
        return new HeartbeatResources(pool, Objects.requireNonNull(wrapper.apply(source)), closed);
    }

    @Override
    public void close() {
        if (closed.compareAndSet(false, true)) pool.close();
    }
}
