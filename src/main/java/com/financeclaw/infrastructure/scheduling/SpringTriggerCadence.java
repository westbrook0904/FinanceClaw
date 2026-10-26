package com.financeclaw.infrastructure.scheduling;

import com.financeclaw.app.scheduling.port.TriggerCadence;
import com.financeclaw.domain.Task.Problem;
import java.time.Instant;
import java.time.ZoneId;
import org.springframework.scheduling.support.CronExpression;

public final class SpringTriggerCadence implements TriggerCadence {
    @Override
    public Instant next(String expression, String zone, Instant after) {
        try {
            var next = CronExpression.parse(expression).next(after.atZone(ZoneId.of(zone)));
            if (next == null) throw new IllegalArgumentException();
            return next.toInstant();
        } catch (IllegalArgumentException | java.time.DateTimeException bad) {
            throw new Problem(422, "invalid_cron_schedule");
        }
    }
}
