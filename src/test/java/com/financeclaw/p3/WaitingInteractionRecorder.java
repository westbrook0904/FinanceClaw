package com.financeclaw.p3;

import com.financeclaw.domain.Governance.Interaction;
import com.financeclaw.infrastructure.governance.GovernanceMapper;
import com.financeclaw.support.PayloadDigest;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import org.apache.ibatis.executor.Executor;
import org.apache.ibatis.mapping.MappedStatement;
import org.apache.ibatis.plugin.Interceptor;
import org.apache.ibatis.plugin.Intercepts;
import org.apache.ibatis.plugin.Invocation;
import org.apache.ibatis.plugin.Signature;
import org.apache.ibatis.session.ResultHandler;
import org.apache.ibatis.session.RowBounds;

/**
 * 只观察通知查询已经映射完成的 Interaction 集合，不额外执行 SQL 或读取 JDBC getter。
 *
 * <p>仅拦截 Executor 的四参数 query，避免将内部六参数调用重复记数；只接受通知改动涉及的两个精确 statement 和模板。
 * 窗口外的通用历史、回答及恢复查询不进入报告。报告保留行数、固定状态计数和 response 字符串 UTF-8 重编码长度， 不保留对象、标识、正文或参数。该长度是映射后逻辑长度，不代表
 * JDBC getter、网络、TOAST、内存分配或吞吐。
 */
@Intercepts(
        @Signature(
                type = Executor.class,
                method = "query",
                args = {MappedStatement.class, Object.class, RowBounds.class, ResultHandler.class}))
final class WaitingInteractionRecorder implements Interceptor {
    static final String HISTORY_ID = GovernanceMapper.class.getName() + ".listInteractionsByTurnId";
    static final String PENDING_ID =
            GovernanceMapper.class.getName() + ".listPendingInteractionsByTurnId";
    static final String HISTORY_SQL =
            "SELECT "
                    + GovernanceMapper.INTERACTION_COLUMNS
                    + " FROM business.interactions WHERE turn_id=? ORDER BY revision,id";
    static final String PENDING_SQL =
            "SELECT "
                    + GovernanceMapper.INTERACTION_COLUMNS
                    + " FROM business.interactions WHERE turn_id=? AND status='pending' ORDER BY revision,id";
    static final String HISTORY_HASH = digest(HISTORY_SQL);
    static final String PENDING_HASH = digest(PENDING_SQL);

    record QueryObservation(
            String templateSha256,
            int rows,
            Map<String, Long> statusCounts,
            long pendingLiveCount,
            long pendingExpiredCount,
            long responseNonNullCount,
            long responseUtf8LogicalBytes) {}

    record Measurement(String phase, List<QueryObservation> queries) {}

    private static final class Recording {
        private final String phase;
        private final List<QueryObservation> queries = new ArrayList<>();

        private Recording(String phase) {
            this.phase = phase;
        }
    }

    private final ThreadLocal<Recording> recording = new ThreadLocal<>();

    void begin(String phase) {
        if (recording.get() != null)
            throw new IllegalStateException("Nested waiting interaction observation");
        recording.set(new Recording(phase));
    }

    Measurement finish() {
        var active = recording.get();
        if (active == null)
            throw new IllegalStateException("Waiting interaction observation is absent");
        recording.remove();
        return new Measurement(active.phase, List.copyOf(active.queries));
    }

    @Override
    public Object intercept(Invocation invocation) throws Throwable {
        var active = recording.get();
        var statement = (MappedStatement) invocation.getArgs()[0];
        String id = statement.getId();
        if (active == null || (!id.equals(HISTORY_ID) && !id.equals(PENDING_ID)))
            return invocation.proceed();
        String template = digest(statement.getBoundSql(invocation.getArgs()[1]).getSql());
        String expected = id.equals(HISTORY_ID) ? HISTORY_HASH : PENDING_HASH;
        if (!template.equals(expected))
            throw new IllegalStateException("Waiting interaction template changed unexpectedly");
        Object returned = invocation.proceed();
        if (!(returned instanceof List<?> rows))
            throw new IllegalStateException("Waiting interaction query did not return a list");
        var statuses = new LinkedHashMap<String, Long>();
        long live = 0;
        long expired = 0;
        long responses = 0;
        long bytes = 0;
        for (var row : rows) {
            if (!(row instanceof Interaction interaction))
                throw new IllegalStateException("Waiting interaction row type changed");
            String status = interaction.status();
            if (!List.of("pending", "answered", "invalid").contains(status))
                throw new IllegalStateException("Unknown waiting interaction status");
            statuses.merge(status, 1L, Long::sum);
            if (status.equals("pending")) {
                if (interaction.live()) live++;
                else expired++;
            }
            if (interaction.response() != null) {
                responses++;
                bytes += interaction.response().getBytes(StandardCharsets.UTF_8).length;
            }
        }
        active.queries.add(
                new QueryObservation(
                        template,
                        rows.size(),
                        Map.copyOf(statuses),
                        live,
                        expired,
                        responses,
                        bytes));
        return returned;
    }

    static String digest(String sql) {
        return PayloadDigest.sha256(sql.replaceAll("\\s+", " ").trim().toLowerCase(Locale.ROOT));
    }
}
