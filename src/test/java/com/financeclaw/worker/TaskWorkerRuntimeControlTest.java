package com.financeclaw.worker;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.Mockito.*;

import com.financeclaw.app.execution.*;
import com.financeclaw.app.runtime.WorkerRegistry;
import com.financeclaw.app.task.WorkSignals;
import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.Task.*;
import java.util.concurrent.*;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;

class TaskWorkerRuntimeControlTest {
    private final ExecutionLeaseService leases = mock(ExecutionLeaseService.class);
    private final RecoverTaskService recovery = mock(RecoverTaskService.class);
    private final ExecuteTaskService execution = mock(ExecuteTaskService.class);
    private final WorkerRegistry registry = mock(WorkerRegistry.class);
    private final ExecutionPolicyService policy = mock(ExecutionPolicyService.class);
    private final Claim claim =
            new Claim(
                    new Command("c", "t", "running", "a", 1, true, null),
                    new Turn(
                            "t", "v", "tenant", "alice", "key", "hash", "input", "release",
                            "running", 2, null, null));

    @Test
    void registryUsesClaimUuidAndRevocationInterruptsWithoutFurtherRenewal() throws Exception {
        var started = new CountDownLatch(1);
        var finish = new CountDownLatch(1);
        when(leases.claim(anyString(), anyLong())).thenReturn(claim);
        when(registry.heartbeat(anyString(), anyInt())).thenReturn(true);
        doAnswer(
                        i -> {
                            started.countDown();
                            assertThat(finish.await(5, TimeUnit.SECONDS)).isTrue();
                            return null;
                        })
                .when(execution)
                .execute(claim);
        try (var worker = worker()) {
            try {
                worker.poll();
                assertThat(started.await(3, TimeUnit.SECONDS)).isTrue();
                var id = ArgumentCaptor.forClass(String.class);
                verify(registry).register(id.capture(), eq(1));
                verify(leases).claim(id.getValue(), 15000);
                doThrow(new Blocked("agent_access_denied")).when(policy).checkDispatch(claim);
                worker.checkRuntimeControl();
                verify(execution).interrupt(claim);
                for (int i = 0; i < 10; i++) worker.heartbeat();
                verify(leases, never()).heartbeat(any(), anyLong());
                worker.checkRuntimeControl();
                verify(policy, times(1)).checkDispatch(claim);
                worker.recover();
                verify(recovery).recoverExpired();
            } finally {
                finish.countDown();
            }
        }
        verify(registry).stop(anyString());
    }

    @Test
    void lostRegistryStopsClaimsAndHeartbeatsButLeavesReceiptRecoveryEnabled() throws Exception {
        var started = new CountDownLatch(1);
        var finish = new CountDownLatch(1);
        when(leases.claim(anyString(), anyLong())).thenReturn(claim);
        doAnswer(
                        i -> {
                            started.countDown();
                            assertThat(finish.await(5, TimeUnit.SECONDS)).isTrue();
                            return null;
                        })
                .when(execution)
                .execute(claim);
        try (var worker = worker()) {
            try {
                worker.poll();
                assertThat(started.await(3, TimeUnit.SECONDS)).isTrue();
                worker.checkRuntimeControl();
                verify(execution).interrupt(claim);
                for (int i = 0; i < 5; i++) {
                    worker.poll();
                    worker.heartbeat();
                    worker.checkRuntimeControl();
                }
                verify(leases, times(1)).claim(anyString(), anyLong());
                verify(leases, never()).heartbeat(any(), anyLong());
                verify(registry, times(1)).register(anyString(), eq(1));
                verify(registry, times(1)).heartbeat(anyString(), eq(1));
                worker.recover();
                verify(recovery).recoverExpired();
                verifyNoInteractions(policy);
            } finally {
                finish.countDown();
            }
        }
    }

    @Test
    void noControlTickPastMonotonicDeadlineStopsHeartbeatAndClaimWithoutRegistryQuery()
            throws Exception {
        var now = new java.util.concurrent.atomic.AtomicLong();
        var started = new CountDownLatch(1);
        var finish = new CountDownLatch(1);
        when(registry.ttlMillis()).thenReturn(1000L);
        when(leases.claim(anyString(), anyLong())).thenReturn(claim);
        doAnswer(
                        i -> {
                            started.countDown();
                            assertThat(finish.await(5, TimeUnit.SECONDS)).isTrue();
                            return null;
                        })
                .when(execution)
                .execute(claim);
        try (var worker = worker(now)) {
            try {
                worker.poll();
                assertThat(started.await(3, TimeUnit.SECONDS)).isTrue();
                // Ordinary scheduler never calls control; only the dedicated heartbeat runs.
                now.set(TimeUnit.MILLISECONDS.toNanos(1000));
                worker.heartbeat();
                verify(execution).interrupt(claim);
                for (int i = 0; i < 5; i++) {
                    worker.poll();
                    worker.heartbeat();
                    worker.checkRuntimeControl();
                }
                verify(leases, times(1)).claim(anyString(), anyLong());
                verify(leases, never()).heartbeat(any(), anyLong());
                verify(registry, never()).heartbeat(anyString(), anyInt());
                verify(registry, times(1)).register(anyString(), eq(1));
                verifyNoInteractions(policy);
                worker.recover();
                verify(recovery).recoverExpired();
            } finally {
                finish.countDown();
            }
        }
    }

    @Test
    void registerLatencyDoesNotExtendLocalTtlFromResponseTime() throws Exception {
        var now = new java.util.concurrent.atomic.AtomicLong();
        var started = new CountDownLatch(1);
        var finish = new CountDownLatch(1);
        when(registry.ttlMillis()).thenReturn(1000L);
        doAnswer(
                        i -> {
                            now.set(TimeUnit.MILLISECONDS.toNanos(800));
                            return null;
                        })
                .when(registry)
                .register(anyString(), eq(1));
        when(leases.claim(anyString(), anyLong())).thenReturn(claim);
        doAnswer(
                        i -> {
                            started.countDown();
                            assertThat(finish.await(5, TimeUnit.SECONDS)).isTrue();
                            return null;
                        })
                .when(execution)
                .execute(claim);
        try (var worker = worker(now)) {
            try {
                worker.poll();
                assertThat(started.await(3, TimeUnit.SECONDS)).isTrue();
                now.set(TimeUnit.MILLISECONDS.toNanos(1000));
                worker.heartbeat();
                verify(execution).interrupt(claim);
                verify(leases, never()).heartbeat(any(), anyLong());
            } finally {
                finish.countDown();
            }
        }
    }

    @Test
    void successfulControlRefreshUsesCallStartAndLateSuccessCannotReviveExpiredWorker()
            throws Exception {
        var now = new java.util.concurrent.atomic.AtomicLong();
        var started = new CountDownLatch(1);
        var finish = new CountDownLatch(1);
        var controlEntered = new CountDownLatch(1);
        var controlRelease = new CountDownLatch(1);
        when(registry.ttlMillis()).thenReturn(1000L);
        when(leases.claim(anyString(), anyLong())).thenReturn(claim);
        doAnswer(
                        i -> {
                            started.countDown();
                            assertThat(finish.await(5, TimeUnit.SECONDS)).isTrue();
                            return null;
                        })
                .when(execution)
                .execute(claim);
        when(registry.heartbeat(anyString(), anyInt()))
                .thenAnswer(
                        i -> {
                            controlEntered.countDown();
                            assertThat(controlRelease.await(5, TimeUnit.SECONDS)).isTrue();
                            return true;
                        });
        try (var worker = worker(now);
                var caller = Executors.newSingleThreadExecutor()) {
            try {
                worker.poll();
                assertThat(started.await(3, TimeUnit.SECONDS)).isTrue();
                now.set(TimeUnit.MILLISECONDS.toNanos(900));
                var pending = caller.submit(worker::checkRuntimeControl);
                assertThat(controlEntered.await(3, TimeUnit.SECONDS)).isTrue();
                now.set(TimeUnit.MILLISECONDS.toNanos(1000));
                worker.heartbeat();
                verify(execution).interrupt(claim);
                controlRelease.countDown();
                pending.get(3, TimeUnit.SECONDS);
                worker.poll();
                worker.heartbeat();
                worker.checkRuntimeControl();
                verify(leases, never()).heartbeat(any(), anyLong());
                verify(leases, times(1)).claim(anyString(), anyLong());
                verify(registry, times(1)).heartbeat(anyString(), anyInt());
                verifyNoInteractions(policy);
            } finally {
                controlRelease.countDown();
                finish.countDown();
            }
        }
    }

    @Test
    void completedControlCallRefreshesOnlyUntilItsStartPlusTtl() throws Exception {
        var now = new java.util.concurrent.atomic.AtomicLong();
        var started = new CountDownLatch(1);
        var finish = new CountDownLatch(1);
        when(registry.ttlMillis()).thenReturn(1000L);
        when(leases.claim(anyString(), anyLong())).thenReturn(claim);
        doAnswer(
                        i -> {
                            started.countDown();
                            assertThat(finish.await(5, TimeUnit.SECONDS)).isTrue();
                            return null;
                        })
                .when(execution)
                .execute(claim);
        when(registry.heartbeat(anyString(), anyInt()))
                .thenAnswer(
                        i -> {
                            now.set(TimeUnit.MILLISECONDS.toNanos(900));
                            return true;
                        });
        try (var worker = worker(now)) {
            try {
                worker.poll();
                assertThat(started.await(3, TimeUnit.SECONDS)).isTrue();
                now.set(TimeUnit.MILLISECONDS.toNanos(100));
                worker.checkRuntimeControl();
                verify(policy).checkDispatch(claim);
                now.set(TimeUnit.MILLISECONDS.toNanos(1100));
                worker.heartbeat();
                verify(execution).interrupt(claim);
                verify(leases, never()).heartbeat(any(), anyLong());
            } finally {
                finish.countDown();
            }
        }
    }

    private TaskWorker worker(java.util.concurrent.atomic.AtomicLong now) {
        return new TaskWorker(
                leases,
                recovery,
                execution,
                new WorkSignals(),
                true,
                1,
                15000,
                2000,
                new IdlePoll(250, 5000),
                registry,
                policy,
                now::get);
    }

    private TaskWorker worker() {
        when(registry.ttlMillis()).thenReturn(15000L);
        return new TaskWorker(
                leases,
                recovery,
                execution,
                new WorkSignals(),
                true,
                1,
                15000,
                2000,
                new IdlePoll(250, 5000),
                registry,
                policy);
    }
}
