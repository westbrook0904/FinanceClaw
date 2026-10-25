package com.financeclaw.p3;

import com.financeclaw.support.PayloadDigest;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Proxy;
import java.sql.CallableStatement;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.SQLException;
import java.sql.Statement;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.atomic.AtomicLong;
import javax.sql.DataSource;
import org.springframework.jdbc.datasource.DelegatingDataSource;

/**
 * 仅供集成测试使用的 JDBC 执行计数器。代理委托真实连接和语句，不代替治理端口或数据库； 只在 execute* 边界记数，prepareStatement、bind、结果读取均不算新的
 * SQL 执行。 记录只含 SQL 模板摘要及类别，不保存 bind 参数、SQL 正文、owner 或工具结果。
 */
final class JdbcExecutionCounter {
    record SqlExecution(String family, String templateSha256, long connection, long thread) {}

    record Measurement(
            String phase,
            int statements,
            Map<String, Long> families,
            int commits,
            int rollbacks,
            List<SqlExecution> executions) {
        long count(String family) {
            return families.getOrDefault(family, 0L);
        }

        long manifests() {
            return count("release_lookup")
                    + count("release_legacy")
                    + count("release_agent_v2")
                    + count("release_agent_v3");
        }
    }

    private static final class Recording {
        final String phase;
        final List<SqlExecution> executions = new ArrayList<>();
        int commits;
        int rollbacks;

        Recording(String phase) {
            this.phase = phase;
        }
    }

    // 此 ThreadLocal 仅隔离测试观察窗口，不承载任何生产权限或事务缓存；子线程必须显式开始自己的观察。
    private final ThreadLocal<Recording> recording = new ThreadLocal<>();
    private final AtomicLong connections = new AtomicLong();

    void begin(String phase) {
        if (recording.get() != null) throw new IllegalStateException("Nested SQL measurement");
        recording.set(new Recording(phase));
    }

    Measurement finish() {
        var current = recording.get();
        if (current == null) throw new IllegalStateException("SQL measurement is absent");
        recording.remove();
        var families = new LinkedHashMap<String, Long>();
        for (var execution : current.executions) {
            families.merge(execution.family(), 1L, Long::sum);
        }
        return new Measurement(
                current.phase,
                current.executions.size(),
                Map.copyOf(families),
                current.commits,
                current.rollbacks,
                List.copyOf(current.executions));
    }

    DataSource wrap(DataSource delegate) {
        return new CountingDataSource(delegate);
    }

    /** 包装主数据源本身，使 MyBatis 与事务管理器共享同一个资源身份和同一个真实池。 */
    private final class CountingDataSource extends DelegatingDataSource implements AutoCloseable {
        CountingDataSource(DataSource delegate) {
            super(delegate);
        }

        @Override
        public Connection getConnection() throws SQLException {
            return connection(super.getConnection());
        }

        @Override
        public Connection getConnection(String username, String password) throws SQLException {
            return connection(super.getConnection(username, password));
        }

        @Override
        public void close() throws Exception {
            if (getTargetDataSource() instanceof AutoCloseable closeable) closeable.close();
        }
    }

    private Connection connection(Connection delegate) {
        long id = connections.incrementAndGet();
        return (Connection)
                Proxy.newProxyInstance(
                        Connection.class.getClassLoader(),
                        new Class<?>[] {Connection.class},
                        (proxy, method, arguments) -> {
                            try {
                                Object result = method.invoke(delegate, arguments);
                                if (result instanceof Statement statement
                                        && (method.getName().equals("prepareStatement")
                                                || method.getName().equals("prepareCall")
                                                || method.getName().equals("createStatement"))) {
                                    String template =
                                            arguments != null
                                                            && arguments.length > 0
                                                            && arguments[0] instanceof String sql
                                                    ? sql
                                                    : null;
                                    return statement(statement, template, id);
                                }
                                var active = recording.get();
                                if (active != null) {
                                    if (method.getName().equals("commit")) active.commits++;
                                    if (method.getName().equals("rollback")) active.rollbacks++;
                                }
                                return result;
                            } catch (InvocationTargetException invoked) {
                                throw invoked.getCause();
                            }
                        });
    }

    private Statement statement(Statement delegate, String preparedSql, long connection) {
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
                            boolean execution =
                                    name.equals("execute")
                                            || name.equals("executeQuery")
                                            || name.equals("executeUpdate")
                                            || name.equals("executeLargeUpdate")
                                            || name.equals("executeBatch")
                                            || name.equals("executeLargeBatch");
                            // 本轮测量路径均是带参数的 PreparedStatement；Statement 的直接 SQL 同样只取模板摘要。
                            String sql =
                                    preparedSql != null
                                            ? preparedSql
                                            : arguments != null
                                                            && arguments.length > 0
                                                            && arguments[0] instanceof String text
                                                    ? text
                                                    : "<jdbc-batch>";
                            try {
                                return method.invoke(delegate, arguments);
                            } catch (InvocationTargetException invoked) {
                                throw invoked.getCause();
                            } finally {
                                if (execution) record(sql, connection);
                            }
                        });
    }

    private void record(String sql, long connection) {
        var active = recording.get();
        if (active == null) return;
        String template = sql.replaceAll("\\s+", " ").trim().toLowerCase(Locale.ROOT);
        active.executions.add(
                new SqlExecution(
                        family(template),
                        PayloadDigest.sha256(template),
                        connection,
                        Thread.currentThread().threadId()));
    }

    /** 使用精确的语句形状区分类别，不能把 child JOIN 中出现的其他表再次计算成单表读取。 */
    private static String family(String sql) {
        // 渠道与领取的观察同样按一次 execute 归类；不能把投影 JOIN 中的 Turn 再记一遍。
        if (sql.startsWith("select b.id from channel.bindings b join business.turns t "))
            return "notice_projection_scan";
        if (sql.startsWith("select ")
                && sql.contains(" from channel.bindings where id=")
                && sql.endsWith("for update")) return "notice_binding_lock";
        if (sql.startsWith("select ")
                && sql.contains(" from channel.notices where status='pending'"))
            return "notice_pending_read";
        if (sql.startsWith("select ")
                && sql.contains(" from channel.notices where id=")
                && sql.endsWith("for update")) return "notice_lock";
        if (sql.startsWith("insert into channel.notices")) return "notice_insert";
        if (sql.startsWith("update channel.notices set status='sending'"))
            return "notice_claim_update";
        if (sql.startsWith("update channel.notices set status='unknown',reason='worker_lost'"))
            return "notice_expired_recovery";
        if (sql.startsWith("update channel.notices set status=")) return "notice_outcome_update";
        if (sql.startsWith("select ") && sql.contains(" from business.interactions where "))
            return "interaction_read";
        if (sql.startsWith("select ")
                && sql.contains(" from business.commands where status='queued'")
                && sql.endsWith("for update skip locked")) return "command_claim_poll";
        if (sql.startsWith("update business.commands set status='running'"))
            return "command_claim_update";
        if (sql.startsWith("insert into business.attempts")) return "attempt_insert";
        if (sql.startsWith("update business.turns set status=")) return "turn_state_update";
        if (sql.startsWith("insert into business.events")) return "event_insert";
        // 一条自关联查询只计一次物理 execute，不能把基础清单 JOIN 再算成一次读取。
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
        if (sql.startsWith("select ")
                && sql.contains(" from business.commands where id=")
                && sql.endsWith("for update")) return "command_lock";
        if (sql.startsWith("select ") && sql.contains(" from business.turns where id="))
            return "turn_read";
        if (sql.startsWith("select ")
                && sql.contains(" from memory.owners where ")
                && sql.endsWith("for update")) return "owner_lock";
        if (sql.startsWith("select ") && sql.contains(" from memory.sources where id="))
            return "source_read";
        if (sql.startsWith("select ") && sql.contains(" from business.turn_policies where "))
            return "policy_read";
        if (sql.startsWith("select ")
                && sql.contains(" from business.grants where ")
                && sql.endsWith("for update")) return "grant_lock";
        if (sql.startsWith("select ") && sql.contains(" from business.child_invocations c "))
            return "child_read";
        if (sql.startsWith("select input_hash,result from business.capability_steps "))
            return "step_read";
        if (sql.startsWith("insert into business.capability_steps")) return "step_insert";
        if (sql.startsWith("update business.child_invocations set tool_calls="))
            return "child_tool_update";
        if (sql.startsWith("update business.child_invocations set model_calls="))
            return "child_model_update";
        if (sql.startsWith("update business.turn_policies set tool_calls="))
            return "root_tool_update";
        if (sql.startsWith("update business.turn_policies set model_calls="))
            return "root_model_update";
        return "other";
    }
}
