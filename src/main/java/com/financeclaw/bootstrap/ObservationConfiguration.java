package com.financeclaw.bootstrap;

import com.financeclaw.infrastructure.runtime.RuntimeObservations;
import io.micrometer.observation.ObservationRegistry;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

/**
 * Uses the configured local ObservationRegistry; exporters remain an explicit deployment choice.
 */
@Configuration(proxyBeanMethods = false)
public class ObservationConfiguration {
    @Bean
    RuntimeObservations runtimeObservations(ObjectProvider<ObservationRegistry> registries) {
        return new RuntimeObservations(registries.getIfAvailable(() -> ObservationRegistry.NOOP));
    }
}
