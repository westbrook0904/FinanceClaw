package com.financeclaw.infrastructure.runtime;

import com.financeclaw.app.task.PersistentEventSignals;
import java.sql.Connection;
import java.util.UUID;
import javax.sql.DataSource;
import org.postgresql.PGConnection;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DataSourceTransactionManager;
import org.springframework.jdbc.datasource.SingleConnectionDataSource;
import org.springframework.transaction.support.TransactionTemplate;

/** 每个业务 JVM 复用既有业务连接预算中的一条连接，不查询空闲消息表。 */
public final class PostgresEventListener implements AutoCloseable {
    private static final String CHANNEL = "financeclaw_task_events";
    private final DataSource source;
    private final PersistentEventSignals signals;
    private final int receiveTimeoutMillis;
    private final long reconnectMillis;
    private volatile boolean running;
    private volatile Connection active;
    private volatile int backendPid;
    private Thread thread;

    public PostgresEventListener(
            DataSource source,
            PersistentEventSignals signals,
            long receiveTimeoutMillis,
            long reconnectMillis) {
        if (receiveTimeoutMillis < 100
                || receiveTimeoutMillis > 5000
                || reconnectMillis < 100
                || reconnectMillis > 30000)
            throw new IllegalArgumentException("invalid_persistent_event_listener_intervals");
        this.source = java.util.Objects.requireNonNull(source);
        this.signals = java.util.Objects.requireNonNull(signals);
        this.receiveTimeoutMillis = (int) receiveTimeoutMillis;
        this.reconnectMillis = reconnectMillis;
    }

    public synchronized void start() {
        if (thread != null) throw new IllegalStateException("event_listener_already_started");
        running = true;
        thread = new Thread(this::listen, "persistent-event-listener");
        thread.setDaemon(true);
        thread.start();
    }

    public int backendPid() {
        return backendPid;
    }

    private void listen() {
        while (running) {
            try {
                Connection connection = source.getConnection();
                active = connection;
                if (!running) break;
                PGConnection postgres = connection.unwrap(PGConnection.class);
                // LISTEN 要在提交后生效；Spring 管理短事务并恢复连接的原自动提交配置。
                // SingleConnectionDataSource 抑制 close，外层 finally 才归还唯一持有连接。
                var held = new SingleConnectionDataSource(connection, true);
                var tx = new TransactionTemplate(new DataSourceTransactionManager(held));
                tx.setTimeout(5);
                tx.executeWithoutResult(
                        status -> new JdbcTemplate(held).execute("LISTEN " + CHANNEL));
                synchronized (this) {
                    if (!running) break;
                    backendPid = postgres.getBackendPID();
                    signals.connected();
                }
                while (running) {
                    // 原生驱动阻塞读取网络通知；timeout 只用于退出检查，不发送 SQL 心跳。
                    var notifications = postgres.getNotifications(receiveTimeoutMillis);
                    if (notifications == null) continue;
                    for (var notification : notifications) {
                        if (!CHANNEL.equals(notification.getName())) continue;
                        String payload = notification.getParameter();
                        if (payload.equals("*")) signals.wake(payload);
                        else {
                            try {
                                if (UUID.fromString(payload).toString().equals(payload))
                                    signals.wake(payload);
                            } catch (IllegalArgumentException ignored) {
                                // 通知是非权威提示；拒绝非 UUID/全局标记，不把任意正文送入应用。
                            }
                        }
                    }
                }
            } catch (Exception ignored) {
                // 不将驱动错误、连接串或数据库消息放进客户端或观测标签。
            } finally {
                backendPid = 0;
                signals.disconnected();
                terminate(active);
                active = null;
            }
            if (running) {
                try {
                    Thread.sleep(reconnectMillis);
                } catch (InterruptedException interrupted) {
                    Thread.currentThread().interrupt();
                    break;
                }
            }
        }
    }

    private static void terminate(Connection connection) {
        if (connection == null) return;
        try {
            connection.abort(Runnable::run);
        } catch (Exception ignored) {
        }
        try {
            connection.close();
        } catch (Exception ignored) {
        }
    }

    @Override
    public void close() {
        Thread worker;
        synchronized (this) {
            running = false;
            worker = thread;
            signals.disconnected();
        }
        if (worker == null) return;
        worker.interrupt();
        // 终止物理连接而不是把 LISTEN 会话直接放回池，且唤醒驱动阻塞读取。
        terminate(active);
        try {
            worker.join(6000);
        } catch (InterruptedException interrupted) {
            Thread.currentThread().interrupt();
        }
    }
}
