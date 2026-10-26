package com.financeclaw.p4;

import static org.assertj.core.api.Assertions.*;

import com.financeclaw.infrastructure.scheduling.SpringTriggerCadence;
import java.time.Instant;
import org.junit.jupiter.api.Test;

class TriggerCadenceTest {
    private final SpringTriggerCadence cron = new SpringTriggerCadence();

    @Test
    void timezoneAndDstFollowDeclaredLocalSchedule() {
        assertThat(cron.next("0 0 9 * * *", "Asia/Shanghai", Instant.parse("2026-10-26T00:59:59Z")))
                .isEqualTo(Instant.parse("2026-10-26T01:00:00Z"));
        // New York skips 02:30 on the spring-forward day.
        assertThat(
                        cron.next(
                                "0 30 2 * * *",
                                "America/New_York",
                                Instant.parse("2026-03-08T06:59:00Z")))
                .isEqualTo(Instant.parse("2026-03-09T06:30:00Z"));
    }

    @Test
    void invalidOrImpossibleSchedulesAndZonesFailBeforePersistence() {
        assertThatThrownBy(() -> cron.next("not-cron", "UTC", Instant.EPOCH))
                .hasMessage("invalid_cron_schedule");
        assertThatThrownBy(() -> cron.next("0 0 * * * *", "wrong-zone", Instant.EPOCH))
                .hasMessage("invalid_cron_schedule");
        assertThatThrownBy(() -> cron.next("0 0 0 31 2 *", "UTC", Instant.EPOCH))
                .hasMessage("invalid_cron_schedule");
    }
}
