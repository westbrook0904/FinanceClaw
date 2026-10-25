package com.financeclaw.p3;

import com.financeclaw.domain.Governance.Interaction;
import com.financeclaw.infrastructure.governance.GovernanceMapper;
import com.financeclaw.support.PayloadDigest;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import org.apache.ibatis.executor.Executor;
import org.apache.ibatis.mapping.MappedStatement;
import org.apache.ibatis.plugin.Interceptor;
import org.apache.ibatis.plugin.Intercepts;
import org.apache.ibatis.plugin.Invocation;
import org.apache.ibatis.plugin.Signature;
import org.apache.ibatis.session.ResultHandler;
import org.apache.ibatis.session.RowBounds;

/**
 * 回答批次完成查询的 test-only 观察器：只读取 MyBatis 已返回的集合，不增加 SQL/getter。
 *
 * <p>旧查询返回整个 Turn 的 Interaction，本观察器按本次窗口的 currentAttempt 重现原 allMatch 判定； 新查询必须返回恰好一个非 null
 * Boolean。attempt 只在窗口内用于比对，不写入报告。 仅保留实体/标量行数、response 非 null 数量及 UTF-8 逻辑长度、hasUnanswered，原集合原样返回。
 * 该长度不代表 JDBC getter、网络、TOAST、内存分配或吞吐。
 */
@Intercepts(
        @Signature(
                type = Executor.class,
                method = "query",
                args = {MappedStatement.class, Object.class, RowBounds.class, ResultHandler.class}))
final class InteractionCompletionRecorder implements Interceptor {
    static final String HISTORY_ID = GovernanceMapper.class.getName() + ".listInteractionsByTurnId";
    static final String EXISTS_ID =
            GovernanceMapper.class.getName() + ".existsUnansweredInteractionsByTurnAndAttempt";
    static final String HISTORY_SQL =
            "SELECT "
                    + GovernanceMapper.INTERACTION_COLUMNS
                    + " FROM business.interactions WHERE turn_id=? ORDER BY revision,id";
    static final String EXISTS_SQL =
            "SELECT EXISTS(SELECT 1 FROM business.interactions WHERE turn_id=? AND attempt_id=? AND status<>'answered')";
    static final String HISTORY_HASH = digest(HISTORY_SQL);
    static final String EXISTS_HASH = digest(EXISTS_SQL);

    record QueryObservation(
            String templateSha256,
            int entityRows,
            int scalarRows,
            long responseNonNullCount,
            long responseUtf8LogicalBytes,
            boolean hasUnanswered) {}

    record Measurement(String phase, List<QueryObservation> queries) {}

    private static final class Recording {
        private final String phase;
        private final String attempt;
        private final List<QueryObservation> queries = new ArrayList<>();

        private Recording(String phase, String attempt) {
            this.phase = phase;
            this.attempt = attempt;
        }
    }

    private final ThreadLocal<Recording> recording = new ThreadLocal<>();

    void begin(String phase, String currentAttempt) {
        if (recording.get() != null)
            throw new IllegalStateException("Nested interaction completion observation");
        if (currentAttempt == null || currentAttempt.isBlank())
            throw new IllegalArgumentException("Completion observation requires an attempt");
        recording.set(new Recording(phase, currentAttempt));
    }

    Measurement finish() {
        var active = recording.get();
        if (active == null)
            throw new IllegalStateException("Interaction completion observation is absent");
        recording.remove();
        return new Measurement(active.phase, List.copyOf(active.queries));
    }

    @Override
    public Object intercept(Invocation invocation) throws Throwable {
        var active = recording.get();
        var statement = (MappedStatement) invocation.getArgs()[0];
        String id = statement.getId();
        if (active == null || (!id.equals(HISTORY_ID) && !id.equals(EXISTS_ID)))
            return invocation.proceed();
        String hash = digest(statement.getBoundSql(invocation.getArgs()[1]).getSql());
        if (!hash.equals(id.equals(HISTORY_ID) ? HISTORY_HASH : EXISTS_HASH))
            throw new IllegalStateException("Unexpected interaction completion template");
        Object returned = invocation.proceed();
        if (!(returned instanceof List<?> rows))
            throw new IllegalStateException("Completion query did not return a list");
        if (id.equals(EXISTS_ID)) {
            if (rows.size() != 1 || !(rows.getFirst() instanceof Boolean value))
                throw new IllegalStateException("Completion EXISTS must return one Boolean");
            active.queries.add(new QueryObservation(hash, 0, 1, 0, 0, value));
        } else {
            long responses = 0;
            long bytes = 0;
            boolean complete = true;
            for (var row : rows) {
                if (!(row instanceof Interaction interaction))
                    throw new IllegalStateException("Completion history row type changed");
                if (active.attempt.equals(interaction.attemptId()))
                    complete &= interaction.status().equals("answered");
                if (interaction.response() != null) {
                    responses++;
                    bytes += interaction.response().getBytes(StandardCharsets.UTF_8).length;
                }
            }
            active.queries.add(
                    new QueryObservation(hash, rows.size(), 0, responses, bytes, !complete));
        }
        return returned;
    }

    static String digest(String sql) {
        return PayloadDigest.sha256(sql.replaceAll("\\s+", " ").trim().toLowerCase(Locale.ROOT));
    }
}
