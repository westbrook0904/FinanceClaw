package com.financeclaw.p3;

import com.financeclaw.support.PayloadDigest;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.lang.reflect.Proxy;
import java.sql.CallableStatement;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.SQLException;
import java.sql.Statement;
import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Objects;
import java.util.Set;
import java.util.TreeMap;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicLong;
import javax.sql.DataSource;
import org.springframework.jdbc.datasource.DelegatingDataSource;

/**
 * 测试专用的全线程 JDBC 观察窗口；不承载生产缓存、授权或事务行为。
 *
 * <p>窗口在借连接入口取得归属，而不是在 execute 返回时猜测当前窗口。归属一直保持到逻辑 Connection.close， 因此结束窗口会等待已借连接的后续 SQL、显式
 * commit/rollback 和归还连接。窗口前已借出的连接可在窗口内首次执行 SQL 时绑定； PreparedStatement 的创建时机不决定归属。一个 recorder
 * 只允许一个尚未完成的窗口。
 *
 * <p>统计的是 JDBC execute* 调用（executeBatch 也只算一次），不是数据库网络往返或批内语句数。 commit/rollback 仅统计显式调用；自动提交和
 * setAutoCommit 产生的隐式提交不在此计数中。借连接耗时包含整个 getConnection 调用，不能解释为精确的连接池排队时间。驱动专有 unwrap API 不属于本
 * fixture 的观察路径。
 *
 * <p>报告不保存 SQL 正文、参数、异常正文、原线程名或 owner。prepared SQL 在创建代理时即转换为固定类别与摘要。 观察失败不会替换 JDBC
 * 的返回值或原始异常；容量溢出、排空超时及关闭失败在 finish 时显式阻止验收。
 */
final class ApplicationJdbcRecorder {
    record Limits(int maxExecutions, int maxTemplates) {
        Limits {
            if (maxExecutions < 1 || maxTemplates < 1) {
                throw new IllegalArgumentException("Recorder limits must be positive");
            }
        }
    }

    record SqlExecution(
            long sequence,
            String family,
            String templateSha256,
            String role,
            long connection,
            long elapsedNanos,
            boolean success) {}

    record BorrowMetrics(
            long attempts,
            long successes,
            long failures,
            long elapsedNanos,
            long maxElapsedNanos) {}

    record Measurement(
            String phase,
            long statements,
            Map<String, Long> families,
            Map<String, Long> roles,
            long commits,
            long rollbacks,
            long commitFailures,
            long rollbackFailures,
            List<SqlExecution> executions,
            BorrowMetrics borrows,
            Instant startedAt,
            Instant captureClosedAt,
            Instant finishedAt,
            Duration elapsed) {
        Measurement {
            families = Collections.unmodifiableMap(new TreeMap<>(families));
            roles = Collections.unmodifiableMap(new TreeMap<>(roles));
            executions = List.copyOf(executions);
        }

        long count(String family) {
            return families.getOrDefault(family, 0L);
        }
    }

    /** 诊断只包含计数及固定状态，不传播 SQL/JDBC 异常正文。 */
    record Diagnostics(
            String phase,
            String state,
            long pendingBorrows,
            long activeConnections,
            long inFlight,
            boolean overflow,
            long closeFailures) {}

    static final class RecorderException extends IllegalStateException {
        private final String reason;
        private final Diagnostics diagnostics;

        RecorderException(String reason, Diagnostics diagnostics) {
            super(reason + ": " + diagnostics);
            this.reason = reason;
            this.diagnostics = diagnostics;
        }

        String reason() {
            return reason;
        }

        Diagnostics diagnostics() {
            return diagnostics;
        }
    }

    private enum State {
        OPEN,
        DRAINING,
        FINISHED
    }

    private enum Kind {
        SQL,
        COMMIT,
        ROLLBACK,
        CLOSE
    }

    private record Template(String family, String sha256) {}

    private record Operation(
            Window window,
            Kind kind,
            long sequence,
            String role,
            long startedNanos,
            Template template) {}

    private final Object monitor = new Object();
    private final Limits limits;
    private final AtomicLong connectionIds = new AtomicLong();
    private Window current;

    ApplicationJdbcRecorder() {
        this(new Limits(20_000, 2_048));
    }

    ApplicationJdbcRecorder(Limits limits) {
        this.limits = Objects.requireNonNull(limits);
    }

    DataSource wrap(DataSource delegate) {
        Objects.requireNonNull(delegate);
        if (delegate instanceof CountingDataSource wrapped && wrapped.recorder() == this) {
            return delegate;
        }
        return new CountingDataSource(delegate);
    }

    Window begin(String phase) {
        if (phase == null || phase.isBlank() || phase.length() > 128) {
            throw new IllegalArgumentException("A short non-sensitive phase label is required");
        }
        synchronized (monitor) {
            if (current != null && current.state != State.FINISHED) {
                throw new RecorderException("unfinished_window", current.diagnostics());
            }
            current = new Window(phase);
            return current;
        }
    }

    final class Window {
        private final String phase;
        private final Instant startedAt = Instant.now();
        private final long startedNanos = System.nanoTime();
        private final List<SqlExecution> executions = new ArrayList<>();
        private final Set<String> templates = new HashSet<>();
        private final Map<String, Long> families = new TreeMap<>();
        private final Map<String, Long> roles = new TreeMap<>();
        private State state = State.OPEN;
        private Instant captureClosedAt;
        private long pendingBorrows;
        private long activeConnections;
        private long inFlight;
        private long nextSequence;
        private long statements;
        private long commits;
        private long rollbacks;
        private long commitFailures;
        private long rollbackFailures;
        private long closeFailures;
        private long borrowAttempts;
        private long borrowSuccesses;
        private long borrowFailures;
        private long borrowElapsedNanos;
        private long borrowMaxElapsedNanos;
        private boolean overflow;
        private Measurement measurement;

        private Window(String phase) {
            this.phase = phase;
        }

        /** 只等待执行调用完成的数量，不意味着事务已经提交，也不认证心跳影响行数。 */
        boolean awaitSqlFamily(String family, long atLeast, Duration timeout) {
            Objects.requireNonNull(family);
            if (atLeast < 0) throw new IllegalArgumentException("Negative SQL threshold");
            long budget = nanos(timeout);
            long start = System.nanoTime();
            synchronized (monitor) {
                while (true) {
                    requireUsable();
                    if (families.getOrDefault(family, 0L) >= atLeast) return true;
                    long remaining = budget - (System.nanoTime() - start);
                    if (state == State.FINISHED || remaining <= 0) return false;
                    waitForChange(remaining);
                }
            }
        }

        /**
         * 先关闭新借连接/首次执行的捕获，再等待原 token 的完整逻辑连接生命周期。排空超时不会生成一个看似成功的部分结果； 窗口保持
         * DRAINING，调用方可先关闭自己的连接，再重试 finish。此期间不允许开始下一样本。
         */
        Measurement finish(Duration timeout) {
            long budget = nanos(timeout);
            long start = System.nanoTime();
            synchronized (monitor) {
                if (state == State.FINISHED) return measurement;
                if (state == State.OPEN) {
                    state = State.DRAINING;
                    captureClosedAt = Instant.now();
                }
                while (pendingBorrows != 0 || activeConnections != 0 || inFlight != 0) {
                    long remaining = budget - (System.nanoTime() - start);
                    if (remaining <= 0) throw failure("drain_timeout");
                    waitForChange(remaining);
                }
                // 另一线程可能已在本线程等待排空期间完成 finish；所有调用返回同一份冻结结果。
                if (state == State.FINISHED) return measurement;
                requireUsable();
                Instant finishedAt = Instant.now();
                var ordered = new ArrayList<>(executions);
                ordered.sort(java.util.Comparator.comparingLong(SqlExecution::sequence));
                measurement =
                        new Measurement(
                                phase,
                                statements,
                                families,
                                roles,
                                commits,
                                rollbacks,
                                commitFailures,
                                rollbackFailures,
                                ordered,
                                new BorrowMetrics(
                                        borrowAttempts,
                                        borrowSuccesses,
                                        borrowFailures,
                                        borrowElapsedNanos,
                                        borrowMaxElapsedNanos),
                                startedAt,
                                captureClosedAt,
                                finishedAt,
                                Duration.ofNanos(System.nanoTime() - startedNanos));
                state = State.FINISHED;
                monitor.notifyAll();
                return measurement;
            }
        }

        private void requireUsable() {
            if (overflow) throw failure("recording_overflow");
            if (closeFailures != 0) throw failure("connection_close_failed");
        }

        private RecorderException failure(String reason) {
            return new RecorderException(reason, diagnostics());
        }

        private Diagnostics diagnostics() {
            return new Diagnostics(
                    phase,
                    state.name(),
                    pendingBorrows,
                    activeConnections,
                    inFlight,
                    overflow,
                    closeFailures);
        }

        private void waitForChange(long remaining) {
            try {
                TimeUnit.NANOSECONDS.timedWait(monitor, remaining);
            } catch (InterruptedException interrupted) {
                Thread.currentThread().interrupt();
                throw failure("recording_interrupted");
            }
        }
    }

    /** 与事务管理器/MyBatis 共用同一 wrapper 资源身份；close 转交原池且不重复关闭。 */
    private final class CountingDataSource extends DelegatingDataSource implements AutoCloseable {
        private final AtomicBoolean closed = new AtomicBoolean();

        private CountingDataSource(DataSource delegate) {
            super(delegate);
        }

        private ApplicationJdbcRecorder recorder() {
            return ApplicationJdbcRecorder.this;
        }

        @Override
        public Connection getConnection() throws SQLException {
            return borrow(() -> super.getConnection());
        }

        @Override
        public Connection getConnection(String username, String password) throws SQLException {
            return borrow(() -> super.getConnection(username, password));
        }

        @Override
        public void close() throws Exception {
            if (closed.compareAndSet(false, true)
                    && getTargetDataSource() instanceof AutoCloseable closeable) {
                closeable.close();
            }
        }
    }

    @FunctionalInterface
    private interface Borrow {
        Connection get() throws SQLException;
    }

    private Connection borrow(Borrow delegate) throws SQLException {
        Window window;
        synchronized (monitor) {
            window = current != null && current.state == State.OPEN ? current : null;
            if (window != null) {
                window.pendingBorrows++;
                window.borrowAttempts++;
            }
        }
        long start = System.nanoTime();
        boolean success = false;
        try {
            Connection result = connection(delegate.get(), window);
            success = true;
            return result;
        } finally {
            synchronized (monitor) {
                if (window != null) {
                    window.pendingBorrows--;
                    long elapsed = System.nanoTime() - start;
                    window.borrowElapsedNanos += elapsed;
                    window.borrowMaxElapsedNanos = Math.max(window.borrowMaxElapsedNanos, elapsed);
                    if (success) {
                        window.borrowSuccesses++;
                        window.activeConnections++;
                    } else {
                        window.borrowFailures++;
                    }
                    monitor.notifyAll();
                }
            }
        }
    }

    private final class ConnectionState {
        private final Connection delegate;
        private final long id = connectionIds.incrementAndGet();
        private Window window;
        private Connection proxy;
        private boolean closing;
        private boolean closed;

        private ConnectionState(Connection delegate, Window window) {
            this.delegate = delegate;
            this.window = window;
        }
    }

    private Connection connection(Connection delegate, Window window) {
        var state = new ConnectionState(delegate, window);
        state.proxy =
                (Connection)
                        Proxy.newProxyInstance(
                                Connection.class.getClassLoader(),
                                new Class<?>[] {Connection.class},
                                (proxy, method, arguments) -> {
                                    String name = method.getName();
                                    if (name.equals("close") || name.equals("abort")) {
                                        return close(state, method, arguments);
                                    }
                                    if (name.equals("unwrap") && arguments[0] == Connection.class) {
                                        return proxy;
                                    }
                                    if (name.equals("isWrapperFor")
                                            && arguments[0] == Connection.class) {
                                        return true;
                                    }
                                    Kind kind =
                                            name.equals("commit")
                                                    ? Kind.COMMIT
                                                    : name.equals("rollback")
                                                            ? Kind.ROLLBACK
                                                            : null;
                                    Operation operation =
                                            kind == null ? null : begin(state, kind, null);
                                    boolean success = false;
                                    try {
                                        Object result = invoke(method, state.delegate, arguments);
                                        success = true;
                                        if (result instanceof Statement statement
                                                && (name.equals("prepareStatement")
                                                        || name.equals("prepareCall")
                                                        || name.equals("createStatement"))) {
                                            Template template =
                                                    arguments != null
                                                                    && arguments.length > 0
                                                                    && arguments[0]
                                                                            instanceof String sql
                                                            ? template(sql)
                                                            : null;
                                            return statement(statement, template, state);
                                        }
                                        return result;
                                    } finally {
                                        complete(operation, state.id, success);
                                    }
                                });
        return state.proxy;
    }

    private Object close(ConnectionState connection, Method method, Object[] arguments)
            throws Throwable {
        Operation operation;
        synchronized (monitor) {
            if (connection.closed || connection.closing) return null;
            connection.closing = true;
            operation = begin(connection, Kind.CLOSE, null);
        }
        boolean success = false;
        try {
            Object result = invoke(method, connection.delegate, arguments);
            success = true;
            return result;
        } finally {
            synchronized (monitor) {
                // 关闭失败保留活跃连接，允许调用方重试 close；该窗口即使随后排空也不能认证为成功。
                if (success) {
                    connection.closed = true;
                    if (connection.window != null) connection.window.activeConnections--;
                }
                connection.closing = false;
                complete(operation, connection.id, success);
                monitor.notifyAll();
            }
        }
    }

    private Statement statement(Statement delegate, Template prepared, ConnectionState connection) {
        Class<?> type =
                delegate instanceof CallableStatement
                        ? CallableStatement.class
                        : delegate instanceof PreparedStatement
                                ? PreparedStatement.class
                                : Statement.class;
        return (Statement)
                Proxy.newProxyInstance(
                        Statement.class.getClassLoader(),
                        new Class<?>[] {type},
                        (proxy, method, arguments) -> {
                            String name = method.getName();
                            if (name.equals("getConnection")) return connection.proxy;
                            if (name.equals("unwrap") && arguments[0] == type) return proxy;
                            if (name.equals("isWrapperFor") && arguments[0] == type) return true;
                            boolean execution =
                                    name.equals("execute")
                                            || name.equals("executeQuery")
                                            || name.equals("executeUpdate")
                                            || name.equals("executeLargeUpdate")
                                            || name.equals("executeBatch")
                                            || name.equals("executeLargeBatch");
                            Template selected =
                                    !execution
                                            ? null
                                            : prepared != null
                                                    ? prepared
                                                    : template(
                                                            arguments != null
                                                                            && arguments.length > 0
                                                                            && arguments[0]
                                                                                    instanceof
                                                                                    String sql
                                                                    ? sql
                                                                    : "<jdbc-batch>");
                            Operation operation =
                                    execution ? begin(connection, Kind.SQL, selected) : null;
                            boolean success = false;
                            try {
                                Object result = invoke(method, delegate, arguments);
                                success = true;
                                return result;
                            } finally {
                                complete(operation, connection.id, success);
                            }
                        });
    }

    private Operation begin(ConnectionState connection, Kind kind, Template template) {
        synchronized (monitor) {
            if (connection.closed) return null;
            if (connection.window == null
                    && !connection.closing
                    && kind == Kind.SQL
                    && current != null
                    && current.state == State.OPEN) {
                connection.window = current;
                current.activeConnections++;
            }
            Window window = connection.window;
            if (window == null) return null;
            window.inFlight++;
            return new Operation(
                    window,
                    kind,
                    kind == Kind.SQL ? ++window.nextSequence : 0,
                    role(Thread.currentThread().getName()),
                    System.nanoTime(),
                    template);
        }
    }

    private void complete(Operation operation, long connection, boolean success) {
        if (operation == null) return;
        synchronized (monitor) {
            Window window = operation.window();
            switch (operation.kind()) {
                case SQL -> {
                    window.statements++;
                    window.families.merge(operation.template().family(), 1L, Long::sum);
                    window.roles.merge(operation.role(), 1L, Long::sum);
                    String hash = operation.template().sha256();
                    if (!window.templates.contains(hash)) {
                        if (window.templates.size() == limits.maxTemplates())
                            window.overflow = true;
                        else window.templates.add(hash);
                    }
                    if (window.executions.size() == limits.maxExecutions()) window.overflow = true;
                    else {
                        window.executions.add(
                                new SqlExecution(
                                        operation.sequence(),
                                        operation.template().family(),
                                        hash,
                                        operation.role(),
                                        connection,
                                        System.nanoTime() - operation.startedNanos(),
                                        success));
                    }
                }
                case COMMIT -> {
                    if (success) window.commits++;
                    else window.commitFailures++;
                }
                case ROLLBACK -> {
                    if (success) window.rollbacks++;
                    else window.rollbackFailures++;
                }
                case CLOSE -> {
                    if (!success) window.closeFailures++;
                }
            }
            window.inFlight--;
            monitor.notifyAll();
        }
    }

    private static Object invoke(Method method, Object target, Object[] arguments)
            throws Throwable {
        try {
            return method.invoke(target, arguments);
        } catch (InvocationTargetException invoked) {
            throw invoked.getCause();
        }
    }

    private static long nanos(Duration timeout) {
        Objects.requireNonNull(timeout);
        if (timeout.isNegative()) throw new IllegalArgumentException("Negative recorder timeout");
        try {
            return timeout.toNanos();
        } catch (ArithmeticException tooLarge) {
            return Long.MAX_VALUE;
        }
    }

    /** 线程来源标签不代表授权身份或业务 owner；未知线程仍计入 other，绝不漏掉异步执行。 */
    private static String role(String name) {
        if (name.startsWith("task-heartbeat-")) return "task_heartbeat";
        if (name.startsWith("task-worker-")) return "task_worker";
        if (name.startsWith("boundedElastic-") || name.startsWith("parallel-"))
            return "execution_async";
        if (name.startsWith("http-") || name.startsWith("https-") || name.startsWith("http-nio-"))
            return "http";
        if (name.startsWith("background-")
                || name.startsWith("scheduling-")
                || name.startsWith("task-scheduler-")) return "background";
        return "other";
    }

    private static Template template(String sql) {
        String normalized = sql.replaceAll("\\s+", " ").trim().toLowerCase(Locale.ROOT);
        return new Template(family(normalized), PayloadDigest.sha256(normalized));
    }

    /** 固定的语句形状标签，不从 SQL 表名/别名生成无限类别；先匹配复合语句以免把 JOIN 算成多个执行。 */
    private static String family(String sql) {
        if (sql.startsWith("with locked_command as materialized")
                && sql.contains("update business.commands c")
                && sql.contains("set lease_until=clock_timestamp()")) return "heartbeat_renew";
        // 自关联的两条目录记录属于一次 SQL 执行；不按 SQL 中出现的表名重复计数。
        if (sql.startsWith(
                        "select r.release_hash, r.manifest_format, r.manifest::text as manifest_json,")
                && sql.contains(
                        " from control_plane.release_manifests r left join control_plane.release_manifests b "))
            return "release_lookup";
        if (sql.startsWith("select manifest::text from control_plane.release_manifests")) {
            if (sql.contains("manifest_format='agent-v3'")) return "release_agent_v3";
            if (sql.contains("manifest_format='agent-v2'")) return "release_agent_v2";
            return "release_legacy";
        }
        if (sql.startsWith("select ") && sql.contains(" from business.commands where ")) {
            if (sql.contains("status='queued'") && sql.contains("skip locked"))
                return "command_claim_poll";
            if (sql.contains("lease_until<=clock_timestamp()")) return "command_expired_poll";
            if (sql.endsWith("for update")) return "command_lock";
        }
        if (sql.startsWith("select ")
                && sql.contains(" from memory.owners where ")
                && sql.endsWith("for update")) return "owner_lock";
        if (sql.startsWith("select ")
                && sql.contains(" from business.grants where ")
                && sql.endsWith("for update")) return "grant_lock";
        if (sql.startsWith("update business.turn_policies set tool_calls="))
            return "root_tool_update";
        if (sql.startsWith("update business.turn_policies set model_calls="))
            return "root_model_update";
        if (sql.startsWith("update business.child_invocations set tool_calls="))
            return "child_tool_update";
        if (sql.startsWith("update business.child_invocations set model_calls="))
            return "child_model_update";
        if (sql.startsWith("select ") && sql.contains(" from business.child_invocations c "))
            return "child_read";
        if (sql.startsWith("update business.turns set status=")) return "turn_state_update";
        if (sql.startsWith("update business.commands set status='done'"))
            return "command_done_update";
        if (sql.startsWith("update business.commands set status='running'"))
            return "command_claim_update";
        if (sql.startsWith("update business.attempts set state_version="))
            return "attempt_seal_update";
        if (sql.equals("<jdbc-batch>")) return "jdbc_batch";
        String[][] tables = {
            {"business.turns", "turn"},
            {"business.commands", "command"},
            {"business.conversations", "conversation"},
            {"business.admission", "admission"},
            {"business.turn_policies", "policy"},
            {"business.grants", "grant"},
            {"business.attempts", "attempt"},
            {"business.events", "event"},
            {"business.journal", "journal"},
            {"business.capability_steps", "step"},
            {"business.child_invocations", "child"},
            {"business.artifacts", "artifact"},
            {"agent_runtime.states", "sdk_state"},
            {"agent_runtime.work_contexts", "work_context"},
            {"memory.owners", "owner"},
            {"memory.sources", "source"},
            {"memory.turn_snapshots", "turn_snapshot"},
            {"memory.user_profiles", "profile"},
            {"memory.facts", "fact"},
            {"memory.projections", "projection"},
            {"memory.jobs", "memory_job"},
            {"control_plane.release_manifests", "release"},
            {"control_plane.releases", "release_base"},
            {"channel.bindings", "channel_binding"},
            {"channel.notices", "outbox"}
        };
        for (String[] table : tables) {
            if (sql.startsWith("select ") && sql.contains(" from " + table[0] + " "))
                return table[1] + "_read";
            if (sql.startsWith("insert into " + table[0] + "(")
                    || sql.startsWith("insert into " + table[0] + " ")) return table[1] + "_insert";
            if (sql.startsWith("update " + table[0] + " ")) return table[1] + "_update";
            if (sql.startsWith("delete from " + table[0] + " ")) return table[1] + "_delete";
        }
        return "other";
    }
}
