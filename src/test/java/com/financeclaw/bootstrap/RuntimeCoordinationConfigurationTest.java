package com.financeclaw.bootstrap;

import static org.assertj.core.api.Assertions.*;

import com.financeclaw.app.task.PersistentEventSignals;
import com.zaxxer.hikari.HikariDataSource;
import org.junit.jupiter.api.Test;
import org.springframework.mock.env.MockEnvironment;

class RuntimeCoordinationConfigurationTest {
    @Test
    void listenerReservationRequiresBusinessCapacityWithoutIncreasingPoolBudget() throws Exception {
        try (var source = new HikariDataSource()) {
            source.setMaximumPoolSize(1);
            var configuration = new RuntimeCoordinationConfiguration();
            assertThatThrownBy(
                            () ->
                                    configuration.persistentEventListener(
                                            source,
                                            new PersistentEventSignals(),
                                            new MockEnvironment()))
                    .hasMessage("persistent_events_require_two_business_connections");
            source.setMaximumPoolSize(2);
            try (var listener =
                    configuration.persistentEventListener(
                            source, new PersistentEventSignals(), new MockEnvironment())) {
                assertThat(source.getMaximumPoolSize()).isEqualTo(2);
                assertThat(source.getHikariPoolMXBean()).isNull();
            }
        }
    }
}
