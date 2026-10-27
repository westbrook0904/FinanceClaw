package com.financeclaw.infrastructure.scheduling;

import com.financeclaw.app.scheduling.port.TriggerCadence;
import com.financeclaw.domain.Task.Problem;
import java.time.Instant;
import java.time.ZoneId;
import org.springframework.scheduling.support.CronExpression;
import org.springframework.util.ConcurrentLruCache;

/** 只复用有界的 Cron 编译结果；时区、计算起点和持久调度事实始终来自本次调用。 */
public final class SpringTriggerCadence implements TriggerCadence {
    private static final int MAX_CACHED_EXPRESSIONS = 256;
    private final ConcurrentLruCache<String, CronExpression> expressions =
            new ConcurrentLruCache<>(MAX_CACHED_EXPRESSIONS, CronExpression::parse);

    @Override
    public Instant next(String expression, String zone, Instant after) {
        try {
            // 保留 CronExpression.parse 对 null 输入的业务错误映射，不交给缓存键校验。
            if (expression == null) {
                throw new IllegalArgumentException();
            }
            var next = expressions.get(expression).next(after.atZone(ZoneId.of(zone)));
            if (next == null) {
                throw new IllegalArgumentException();
            }
            return next.toInstant();
        } catch (IllegalArgumentException | java.time.DateTimeException bad) {
            throw new Problem(422, "invalid_cron_schedule");
        }
    }
}
