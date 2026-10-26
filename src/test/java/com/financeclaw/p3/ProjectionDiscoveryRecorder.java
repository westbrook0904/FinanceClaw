package com.financeclaw.p3;

import com.financeclaw.infrastructure.channel.FeishuMapper;
import com.financeclaw.support.PayloadDigest;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;
import org.apache.ibatis.executor.Executor;
import org.apache.ibatis.mapping.MappedStatement;
import org.apache.ibatis.plugin.Interceptor;
import org.apache.ibatis.plugin.Intercepts;
import org.apache.ibatis.plugin.Invocation;
import org.apache.ibatis.plugin.Signature;
import org.apache.ibatis.session.ResultHandler;
import org.apache.ibatis.session.RowBounds;

/**
 * test-only discovery 返回集合观察器和显式维护入口的一次性“扫描已完成、投影尚未加锁”屏障。 只读取 MyBatis 已经返回的候选数量，不新增
 * SQL/getter，不替代候选或治理结果。 binding ID 仅在屏障内确认指定真实候选已经被扫描，绝不写入报告。定时查询返回时已持绑定锁，仅观察其 0/1 条结果，不触发此屏障。
 */
@Intercepts(
        @Signature(
                type = Executor.class,
                method = "query",
                args = {MappedStatement.class, Object.class, RowBounds.class, ResultHandler.class}))
final class ProjectionDiscoveryRecorder implements Interceptor {
    static final String STATEMENT =
            FeishuMapper.class.getName() + ".listBindingIdsRequiringProjection";
    static final String LOCKED_STATEMENT =
            FeishuMapper.class.getName() + ".getNextBindingRequiringProjectionForUpdateSkipLocked";
    static final String OLD_SQL =
            "SELECT b.id FROM channel.bindings b JOIN business.turns t ON t.id=b.active_turn WHERE NOT b.muted AND t.status IN ('waiting','blocked','completed','failed','cancelled') AND NOT EXISTS(SELECT 1 FROM channel.notices n WHERE n.turn_id=t.id AND n.revision=t.revision AND n.status<>'suppressed') ORDER BY t.created_at LIMIT 20";
    static final String NEW_SQL =
            "SELECT b.id FROM channel.bindings b JOIN business.turns t ON t.id=b.active_turn WHERE NOT b.muted AND t.status IN ('waiting','blocked','completed','failed','cancelled') AND (t.status<>'waiting' OR EXISTS(SELECT 1 FROM business.interactions i WHERE i.turn_id=t.id AND i.status='pending' AND i.expires_at>clock_timestamp())) AND NOT EXISTS(SELECT 1 FROM channel.notices n WHERE n.turn_id=t.id AND n.revision=t.revision AND n.status<>'suppressed') ORDER BY t.created_at LIMIT 20";
    static final String OLD_HASH = digest(OLD_SQL);
    static final String NEW_HASH = digest(NEW_SQL);

    record QueryObservation(String templateSha256, int candidates) {}

    record Measurement(String phase, List<QueryObservation> queries) {}

    private static final class Recording {
        private final String phase;
        private final List<QueryObservation> queries = new ArrayList<>();

        private Recording(String phase) {
            this.phase = phase;
        }
    }

    final class Gate implements AutoCloseable {
        private final String expectedBinding;
        private final CountDownLatch entered = new CountDownLatch(1);
        private final CountDownLatch release = new CountDownLatch(1);

        private Gate(String expectedBinding) {
            this.expectedBinding = expectedBinding;
        }

        void awaitEntered() throws InterruptedException {
            if (!entered.await(5, TimeUnit.SECONDS))
                throw new AssertionError("Discovery did not reach the before-project barrier");
        }

        void release() {
            release.countDown();
        }

        private void hold(List<?> candidates) throws InterruptedException {
            if (!candidates.contains(expectedBinding))
                throw new AssertionError("The real live binding was absent from discovery");
            entered.countDown();
            if (!release.await(5, TimeUnit.SECONDS))
                throw new AssertionError("Discovery barrier was not released");
        }

        @Override
        public void close() {
            gate.compareAndSet(this, null);
            release();
        }
    }

    private final java.util.Set<String> scheduledHashes = new java.util.HashSet<>();

    void registerScheduledTemplate(String sql) {
        scheduledHashes.add(digest(sql));
    }

    private final ThreadLocal<Recording> recording = new ThreadLocal<>();
    private final AtomicReference<Gate> gate = new AtomicReference<>();

    void begin(String phase) {
        if (recording.get() != null)
            throw new IllegalStateException("Nested discovery observation");
        recording.set(new Recording(phase));
    }

    Measurement finish() {
        var active = recording.get();
        if (active == null) throw new IllegalStateException("Discovery observation is absent");
        recording.remove();
        return new Measurement(active.phase, List.copyOf(active.queries));
    }

    Gate pauseAfterDiscovery(String expectedBinding) {
        var installed = new Gate(expectedBinding);
        if (!gate.compareAndSet(null, installed))
            throw new IllegalStateException("A discovery barrier is already installed");
        return installed;
    }

    @Override
    public Object intercept(Invocation invocation) throws Throwable {
        var statement = (MappedStatement) invocation.getArgs()[0];
        boolean maintenance = statement.getId().equals(STATEMENT);
        boolean scheduled = statement.getId().equals(LOCKED_STATEMENT);
        if (!maintenance && !scheduled) return invocation.proceed();
        String hash = digest(statement.getBoundSql(invocation.getArgs()[1]).getSql());
        if (!(maintenance
                ? List.of(OLD_HASH, NEW_HASH).contains(hash)
                : scheduledHashes.contains(hash)))
            throw new IllegalStateException("Unexpected discovery template");
        Object returned = invocation.proceed();
        if (!(returned instanceof List<?> candidates)
                || candidates.stream()
                        .anyMatch(
                                row ->
                                        maintenance
                                                ? !(row instanceof String)
                                                : !(row
                                                        instanceof
                                                        com.financeclaw.domain.Feishu.Binding)))
            throw new IllegalStateException("Unexpected discovery row type");
        var active = recording.get();
        if (active != null) active.queries.add(new QueryObservation(hash, candidates.size()));
        if (maintenance) {
            var pending = gate.getAndSet(null);
            if (pending != null) pending.hold(candidates);
        }
        return returned;
    }

    static String digest(String sql) {
        return PayloadDigest.sha256(sql.replaceAll("\\s+", " ").trim().toLowerCase(Locale.ROOT));
    }
}
