package com.financeclaw.bootstrap;

import com.financeclaw.worker.TaskWorker;
import org.springframework.boot.actuate.health.Health;
import org.springframework.boot.actuate.health.HealthIndicator;
import org.springframework.stereotype.Component;

/** Local readiness only: never refreshes registration, queries SQL or interrupts execution. */
@Component("workerReadinessHealthIndicator")
public final class WorkerReadinessHealth implements HealthIndicator {
    private final TaskWorker worker;

    public WorkerReadinessHealth(TaskWorker worker) {
        this.worker = worker;
    }

    @Override
    public Health health() {
        // No process identity, deadline or exception details are exposed through health endpoints.
        return worker.isLocallyReady() ? Health.up().build() : Health.down().build();
    }
}
