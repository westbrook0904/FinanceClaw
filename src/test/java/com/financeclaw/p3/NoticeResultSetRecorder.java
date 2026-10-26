package com.financeclaw.p3;

import com.financeclaw.support.PayloadDigest;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Proxy;
import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Statement;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.IdentityHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import javax.sql.DataSource;
import org.springframework.jdbc.datasource.DelegatingDataSource;

/**
 * 仅供测试使用的通知结果观察器。只跟踪两个精确候选模板、维护锁定 CTE 和由生产 BoundSQL 注册的定时领取模板；普通 Turn.result 不在测量范围。 不预读 next，不额外取
 * payload，只对生产 getString 已经返回的字符串计算 UTF-8 重编码长度。 此长度不是 PG wire 字节、TOAST 访问页、内存占用或吞吐指标。边界是当前 mapper
 * 的 forward-only PreparedStatement/getString 路径；不声称观测 unwrap 后直接使用原生驱动句柄的调用。
 */
final class NoticeResultSetRecorder {
    static final String OLD_CANDIDATE =
            "ef9ebaee7778d17b78bac27823414dc114a5675bb6f8ee170d2a2a17012373b6";
    static final String NEW_CANDIDATE =
            "b59934a94b90f21a30fbdbc4f2cd88acf391ed696917984abae5dc6f2f05172b";
    static final String LOCKED_NOTICE =
            "71d8cd8cd8410e7da9fda913e09f409fec06cddb5db0f8b59e1380efbcd08e79";
    private static final Map<String, String> TEMPLATES =
            Map.of(
                    OLD_CANDIDATE,
                    "candidate",
                    NEW_CANDIDATE,
                    "candidate",
                    LOCKED_NOTICE,
                    "locked_notice");

    record ResultObservation(
            String kind,
            String templateSha256,
            boolean resultSetObserved,
            List<String> columns,
            int rows,
            int payloadCellsRead,
            long payloadUtf8Bytes) {}

    record Measurement(String phase, List<ResultObservation> results) {}

    private static final class Query {
        private final String kind;
        private final String templateSha256;
        private boolean observed;
        private List<String> columns = List.of();
        private int rows;
        private int payloadCellsRead;
        private long payloadUtf8Bytes;
        private final Set<Long> readCells = new HashSet<>();

        private Query(String kind, String templateSha256) {
            this.kind = kind;
            this.templateSha256 = templateSha256;
        }

        private ResultObservation snapshot() {
            return new ResultObservation(
                    kind,
                    templateSha256,
                    observed,
                    columns,
                    rows,
                    payloadCellsRead,
                    payloadUtf8Bytes);
        }
    }

    private static final class Recording {
        private final String phase;
        private final List<Query> queries = new ArrayList<>();

        private Recording(String phase) {
            this.phase = phase;
        }
    }

    private final Set<String> scheduledTemplates = new HashSet<>();

    /** 由实际 MyBatis BoundSQL 注册有限的 exclude 个数模板，不以宽泛 SQL 模式冒充精确观测。 */
    void registerScheduledTemplate(String sql) {
        scheduledTemplates.add(
                PayloadDigest.sha256(sql.replaceAll("\\s+", " ").trim().toLowerCase(Locale.ROOT)));
    }

    private final ThreadLocal<Recording> recording = new ThreadLocal<>();

    void begin(String phase) {
        if (recording.get() != null)
            throw new IllegalStateException("Nested notice result observation");
        recording.set(new Recording(phase));
    }

    Measurement finish() {
        var active = recording.get();
        if (active == null) throw new IllegalStateException("Notice result observation is absent");
        recording.remove();
        return new Measurement(active.phase, active.queries.stream().map(Query::snapshot).toList());
    }

    DataSource wrap(DataSource delegate) {
        return new RecordingSource(delegate);
    }

    private final class RecordingSource extends DelegatingDataSource implements AutoCloseable {
        private RecordingSource(DataSource delegate) {
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
        return (Connection)
                Proxy.newProxyInstance(
                        Connection.class.getClassLoader(),
                        new Class<?>[] {Connection.class},
                        (proxy, method, arguments) -> {
                            try {
                                Object result = method.invoke(delegate, arguments);
                                if (result instanceof PreparedStatement statement
                                        && method.getName().equals("prepareStatement")
                                        && arguments != null
                                        && arguments[0] instanceof String sql) {
                                    return statement((Connection) proxy, statement, sql);
                                }
                                return result;
                            } catch (InvocationTargetException invoked) {
                                throw invoked.getCause();
                            }
                        });
    }

    private PreparedStatement statement(Connection owner, PreparedStatement delegate, String sql) {
        String hash =
                PayloadDigest.sha256(sql.replaceAll("\\s+", " ").trim().toLowerCase(Locale.ROOT));
        String knownKind = TEMPLATES.get(hash);
        final String kind =
                knownKind != null
                        ? knownKind
                        : scheduledTemplates.contains(hash) ? "scheduled_locked_notice" : null;
        if (kind == null) return delegate;
        class State {
            private Query query;
            private final Map<ResultSet, ResultSet> wrapped = new IdentityHashMap<>();

            private ResultSet result(ResultSet original, Statement statementOwner)
                    throws SQLException {
                if (query == null) return original;
                var existing = wrapped.get(original);
                if (existing != null) return existing;
                if (query.observed)
                    throw new IllegalStateException("Unexpected multiple notice result sets");
                var metadata = original.getMetaData();
                var labels = new ArrayList<String>();
                int width = metadata.getColumnCount();
                for (int i = 1; i <= width; i++) {
                    labels.add(metadata.getColumnLabel(i).toLowerCase(Locale.ROOT));
                }
                query.columns = List.copyOf(labels);
                query.observed = true;
                Query captured = query;
                String payload = captured.kind.equals("candidate") ? "payload" : "notice_payload";
                int payloadIndex = labels.indexOf(payload) + 1;
                var result =
                        (ResultSet)
                                Proxy.newProxyInstance(
                                        ResultSet.class.getClassLoader(),
                                        new Class<?>[] {ResultSet.class},
                                        (proxy, method, arguments) -> {
                                            if (method.getName().equals("getStatement"))
                                                return statementOwner;
                                            try {
                                                Object returned =
                                                        method.invoke(original, arguments);
                                                if (method.getName().equals("next")
                                                        && Boolean.TRUE.equals(returned))
                                                    captured.rows++;
                                                if (method.getName().equals("getString")
                                                        && payloadIndex > 0
                                                        && arguments != null) {
                                                    int index =
                                                            arguments[0] instanceof Integer position
                                                                    ? position
                                                                    : arguments[0]
                                                                                    instanceof
                                                                                    String label
                                                                            ? labels.indexOf(
                                                                                            label
                                                                                                    .toLowerCase(
                                                                                                            Locale
                                                                                                                    .ROOT))
                                                                                    + 1
                                                                            : 0;
                                                    if (index == payloadIndex) {
                                                        if (captured.rows == 0)
                                                            throw new IllegalStateException(
                                                                    "Payload getter preceded production next");
                                                        long cell =
                                                                ((long) captured.rows << 32)
                                                                        | index;
                                                        // 同一个 ResultSet
                                                        // 的同一行列只算一次；列名/序号访问与重复取结果集不虚增长度。
                                                        if (captured.readCells.add(cell)) {
                                                            captured.payloadCellsRead++;
                                                            if (returned instanceof String text)
                                                                captured.payloadUtf8Bytes +=
                                                                        text.getBytes(
                                                                                        StandardCharsets
                                                                                                .UTF_8)
                                                                                .length;
                                                        }
                                                    }
                                                }
                                                return returned;
                                            } catch (InvocationTargetException invoked) {
                                                throw invoked.getCause();
                                            }
                                        });
                wrapped.put(original, result);
                return result;
            }
        }
        var state = new State();
        return (PreparedStatement)
                Proxy.newProxyInstance(
                        Statement.class.getClassLoader(),
                        new Class<?>[] {PreparedStatement.class},
                        (proxy, method, arguments) -> {
                            String name = method.getName();
                            if (name.equals("getConnection")) return owner;
                            if (name.equals("execute") || name.equals("executeQuery")) {
                                var active = recording.get();
                                state.query = active == null ? null : new Query(kind, hash);
                                state.wrapped.clear();
                                if (active != null) active.queries.add(state.query);
                            }
                            try {
                                Object result = method.invoke(delegate, arguments);
                                // MyBatis 常用 execute() 后 getResultSet()；executeQuery() 路径也必须透明支持。
                                if (result instanceof ResultSet rows
                                        && (name.equals("executeQuery")
                                                || name.equals("getResultSet"))) {
                                    return state.result(rows, (Statement) proxy);
                                }
                                return result;
                            } catch (InvocationTargetException invoked) {
                                throw invoked.getCause();
                            }
                        });
    }
}
