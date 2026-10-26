package com.financeclaw.bootstrap;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.Mockito.*;

import com.financeclaw.app.execution.ExecuteTaskService;
import com.financeclaw.app.execution.ExecutionLeaseService;
import com.financeclaw.app.execution.ExecutionPolicyService;
import com.financeclaw.app.execution.RecoverTaskService;
import com.financeclaw.app.runtime.WorkerRegistry;
import com.financeclaw.app.task.WorkSignals;
import com.financeclaw.worker.TaskWorker;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.springframework.boot.actuate.health.Status;

class WorkerReadinessHealthTest {
    @ParameterizedTest
    @ValueSource(booleans = {false, true})
    void probeOnlyObservesLocalReadinessAndNeverExposesWorkerDetails(boolean ready) {
        var worker = mock(TaskWorker.class);
        when(worker.isLocallyReady()).thenReturn(ready);

        var health = new WorkerReadinessHealth(worker).health();

        assertThat(health.getStatus()).isEqualTo(ready ? Status.UP : Status.DOWN);
        assertThat(health.getDetails()).isEmpty();
        verify(worker).isLocallyReady();
        verifyNoMoreInteractions(worker);
    }

    @Test
    void enabledWorkerBeforeFirstRegistrationIsDownWithoutRegisteringOrQuerying() throws Exception {
        assertUnregisteredWorkerReadiness(true, Status.DOWN);
    }

    @Test
    void explicitlyDisabledWorkerStaysUpWithoutRegisteringOrQuerying() throws Exception {
        assertUnregisteredWorkerReadiness(false, Status.UP);
    }

    private static void assertUnregisteredWorkerReadiness(boolean enabled, Status expected)
            throws Exception {
        var leases = mock(ExecutionLeaseService.class);
        var recovery = mock(RecoverTaskService.class);
        var execution = mock(ExecuteTaskService.class);
        var registry = mock(WorkerRegistry.class);
        var policy = mock(ExecutionPolicyService.class);
        when(registry.ttlMillis()).thenReturn(15000L);
        try (var worker =
                new TaskWorker(
                        leases,
                        recovery,
                        execution,
                        new WorkSignals(),
                        enabled,
                        1,
                        15000,
                        2000,
                        250,
                        5000,
                        registry,
                        policy)) {
            // Construction reads the configured TTL; probing must not invoke registration or SQL.
            verify(registry).ttlMillis();
            clearInvocations(registry);
            var indicator = new WorkerReadinessHealth(worker);
            for (int i = 0; i < 3; i++) {
                var health = indicator.health();
                assertThat(health.getStatus()).isEqualTo(expected);
                assertThat(health.getDetails()).isEmpty();
            }
            verifyNoInteractions(leases, recovery, execution, registry, policy);
        }
    }
}
