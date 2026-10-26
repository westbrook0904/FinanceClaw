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
import org.springframework.dao.DataAccessResourceFailureException;
import org.springframework.transaction.CannotCreateTransactionException;

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
                .execute(eq(claim), any());
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
                .execute(eq(claim), any());
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
                .execute(eq(claim), any());
        try (var worker = worker(now)) {
            try {
                worker.poll();
                assertThat(started.await(3, TimeUnit.SECONDS)).isTrue();
                // Ordinary scheduler never calls control; only the dedicated heartbeat runs.
                now.set(TimeUnit.MILLISECONDS.toNanos(1000));
                assertThat(worker.isLocallyReady()).isFalse();
                verify(execution, never()).interrupt(any());
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
                .execute(eq(claim), any());
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
                .execute(eq(claim), any());
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
                .execute(eq(claim), any());
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

    @Test
    void temporaryDatabaseHoldRecoversNewClaimsButNeverRevivesDeniedAttempts() throws Exception {
        var now = new java.util.concurrent.atomic.AtomicLong();
        when(registry.ttlMillis()).thenReturn(1000L);
        var started = new CountDownLatch(1);
        var finish = new CountDownLatch(1);
        var nextStarted = new CountDownLatch(1);
        Claim next = nextClaim();
        when(leases.claim(anyString(), anyLong())).thenReturn(claim, next, null);
        doAnswer(
                        i -> {
                            started.countDown();
                            assertThat(finish.await(5, TimeUnit.SECONDS)).isTrue();
                            return null;
                        })
                .when(execution)
                .execute(eq(claim), any());
        doAnswer(
                        i -> {
                            nextStarted.countDown();
                            return null;
                        })
                .when(execution)
                .execute(eq(next), any());
        when(registry.heartbeat(anyString(), anyInt()))
                .thenThrow(new DataAccessResourceFailureException("database unavailable"))
                .thenReturn(true);
        try (var worker = worker(now)) {
            try {
                assertThat(worker.isLocallyReady()).isFalse();
                worker.poll();
                assertThat(started.await(3, TimeUnit.SECONDS)).isTrue();
                assertThat(worker.isLocallyReady()).isTrue();
                now.set(TimeUnit.MILLISECONDS.toNanos(100));
                worker.checkRuntimeControl();
                assertThat(worker.isLocallyReady()).isFalse();
                verify(execution).interrupt(claim);
                for (int i = 0; i < 5; i++) {
                    worker.poll();
                    worker.heartbeat();
                }
                verify(leases, times(1)).claim(anyString(), anyLong());
                verify(leases, never()).heartbeat(any(), anyLong());
                now.set(TimeUnit.MILLISECONDS.toNanos(200));
                worker.checkRuntimeControl();
                assertThat(worker.isLocallyReady()).isTrue();
                worker.heartbeat();
                worker.checkRuntimeControl();
                verify(leases, never()).heartbeat(eq(claim), anyLong());
                verify(policy, never()).checkDispatch(claim);
                verify(execution, times(1)).interrupt(claim);
                finish.countDown();
                org.awaitility.Awaitility.await()
                        .atMost(java.time.Duration.ofSeconds(3))
                        .until(
                                () -> {
                                    worker.poll();
                                    return nextStarted.getCount() == 0;
                                });
                verify(execution).execute(eq(next), any());
                var id = ArgumentCaptor.forClass(String.class);
                verify(registry, times(1)).register(id.capture(), eq(1));
                verify(leases, atLeast(2)).claim(id.getValue(), 15000);
            } finally {
                finish.countDown();
            }
        }
    }

    @org.junit.jupiter.params.ParameterizedTest
    @org.junit.jupiter.params.provider.ValueSource(booleans = {false, true})
    void initialRegistrationDatabaseFailureHoldsUntilControlCanSafelyRetry(
            boolean transactionFailure) throws Exception {
        var now = new java.util.concurrent.atomic.AtomicLong();
        when(registry.ttlMillis()).thenReturn(1000L);
        RuntimeException unavailable =
                transactionFailure
                        ? new CannotCreateTransactionException("database unavailable")
                        : new DataAccessResourceFailureException("database unavailable");
        doThrow(unavailable).doNothing().when(registry).register(anyString(), eq(1));
        when(registry.heartbeat(anyString(), anyInt())).thenReturn(true);
        try (var worker = worker(now)) {
            worker.poll();
            assertThat(worker.isLocallyReady()).isFalse();
            for (int i = 0; i < 5; i++) {
                worker.poll();
                worker.heartbeat();
            }
            verify(registry, times(1)).register(anyString(), eq(1));
            verifyNoInteractions(leases);
            now.set(TimeUnit.MILLISECONDS.toNanos(100));
            worker.checkRuntimeControl();
            assertThat(worker.isLocallyReady()).isTrue();
            worker.poll();
            var ids = ArgumentCaptor.forClass(String.class);
            verify(registry, times(2)).register(ids.capture(), eq(1));
            assertThat(ids.getAllValues()).containsOnly(ids.getValue());
            verify(leases).claim(ids.getValue(), 15000);
        }
    }

    @Test
    void temporaryHoldCannotExtendTheLastSuccessfulDeadline() throws Exception {
        var now = new java.util.concurrent.atomic.AtomicLong();
        when(registry.ttlMillis()).thenReturn(1000L);
        when(registry.heartbeat(anyString(), anyInt()))
                .thenThrow(new DataAccessResourceFailureException("database unavailable"))
                .thenReturn(true);
        try (var worker = worker(now)) {
            worker.poll();
            now.set(TimeUnit.MILLISECONDS.toNanos(100));
            worker.checkRuntimeControl();
            assertThat(worker.isLocallyReady()).isFalse();
            now.set(TimeUnit.MILLISECONDS.toNanos(1000));
            for (int i = 0; i < 5; i++) {
                worker.checkRuntimeControl();
                worker.poll();
                worker.heartbeat();
            }
            assertThat(worker.isLocallyReady()).isFalse();
            verify(registry, times(1)).register(anyString(), eq(1));
            verify(registry, times(1)).heartbeat(anyString(), anyInt());
            verify(leases, times(1)).claim(anyString(), anyLong());
            verify(leases, never()).heartbeat(any(), anyLong());
            worker.recover();
            verify(recovery).recoverExpired();
        }
    }

    @Test
    void lateControlSuccessCannotReviveOldDeadlineEvenWithoutAnotherSchedulerTick()
            throws Exception {
        var now = new java.util.concurrent.atomic.AtomicLong();
        when(registry.ttlMillis()).thenReturn(1000L);
        when(registry.heartbeat(anyString(), anyInt()))
                .thenAnswer(
                        i -> {
                            now.set(TimeUnit.MILLISECONDS.toNanos(1000));
                            return true;
                        });
        try (var worker = worker(now)) {
            worker.poll();
            now.set(TimeUnit.MILLISECONDS.toNanos(900));
            worker.checkRuntimeControl();
            assertThat(worker.isLocallyReady()).isFalse();
            worker.poll();
            worker.heartbeat();
            worker.checkRuntimeControl();
            verify(registry, times(1)).heartbeat(anyString(), anyInt());
            verify(registry, times(1)).register(anyString(), eq(1));
            verify(leases, times(1)).claim(anyString(), anyLong());
        }
    }

    @Test
    void claimStartedBeforeDatabaseHoldCannotExecuteAfterControlRecovers() throws Exception {
        var now = new java.util.concurrent.atomic.AtomicLong();
        when(registry.ttlMillis()).thenReturn(1000L);
        var claimEntered = new CountDownLatch(1);
        var claimReleased = new CountDownLatch(1);
        when(leases.claim(anyString(), anyLong()))
                .thenAnswer(
                        i -> {
                            claimEntered.countDown();
                            assertThat(claimReleased.await(5, TimeUnit.SECONDS)).isTrue();
                            return claim;
                        });
        when(registry.heartbeat(anyString(), anyInt()))
                .thenThrow(new DataAccessResourceFailureException("database unavailable"))
                .thenReturn(true);
        try (var worker = worker(now);
                var caller = Executors.newSingleThreadExecutor()) {
            try {
                var pending = caller.submit(worker::poll);
                assertThat(claimEntered.await(3, TimeUnit.SECONDS)).isTrue();
                now.set(TimeUnit.MILLISECONDS.toNanos(100));
                worker.checkRuntimeControl();
                assertThat(worker.isLocallyReady()).isFalse();
                now.set(TimeUnit.MILLISECONDS.toNanos(200));
                worker.checkRuntimeControl();
                assertThat(worker.isLocallyReady()).isTrue();
                claimReleased.countDown();
                pending.get(3, TimeUnit.SECONDS);
                verify(execution, never()).execute(any(), any());
                worker.heartbeat();
                verify(leases, never()).heartbeat(any(), anyLong());
            } finally {
                claimReleased.countDown();
            }
        }
    }

    @org.junit.jupiter.params.ParameterizedTest
    @org.junit.jupiter.params.provider.ValueSource(booleans = {false, true})
    void staleSuccessfulControlCannotClearNewerDatabaseHoldOrExtendItsDeadline(boolean alreadyHeld)
            throws Exception {
        var now = new java.util.concurrent.atomic.AtomicLong();
        when(registry.ttlMillis()).thenReturn(1000L);
        when(registry.heartbeat(anyString(), anyInt())).thenReturn(true);
        var claimEntered = new CountDownLatch(1);
        var claimRelease = new CountDownLatch(1);
        var controlEntered = new CountDownLatch(1);
        var controlRelease = new CountDownLatch(1);
        when(leases.claim(anyString(), anyLong()))
                .thenAnswer(
                        i -> {
                            claimEntered.countDown();
                            assertThat(claimRelease.await(5, TimeUnit.SECONDS)).isTrue();
                            throw new DataAccessResourceFailureException(
                                    "late claim database failure");
                        });
        try (var worker = worker(now);
                var callers = Executors.newFixedThreadPool(2)) {
            try {
                worker.checkRuntimeControl();
                var pendingClaim = callers.submit(worker::poll);
                assertThat(claimEntered.await(3, TimeUnit.SECONDS)).isTrue();
                if (alreadyHeld) {
                    now.set(TimeUnit.MILLISECONDS.toNanos(100));
                    when(registry.heartbeat(anyString(), anyInt()))
                            .thenThrow(
                                    new DataAccessResourceFailureException(
                                            "first database failure"));
                    worker.checkRuntimeControl();
                    assertThat(worker.isLocallyReady()).isFalse();
                }
                doAnswer(
                                i -> {
                                    controlEntered.countDown();
                                    assertThat(controlRelease.await(5, TimeUnit.SECONDS)).isTrue();
                                    return true;
                                })
                        .when(registry)
                        .heartbeat(anyString(), anyInt());
                now.set(TimeUnit.MILLISECONDS.toNanos(200));
                var pendingControl = callers.submit(worker::checkRuntimeControl);
                assertThat(controlEntered.await(3, TimeUnit.SECONDS)).isTrue();
                now.set(TimeUnit.MILLISECONDS.toNanos(300));
                claimRelease.countDown();
                pendingClaim.get(3, TimeUnit.SECONDS);
                assertThat(worker.isLocallyReady()).isFalse();
                now.set(TimeUnit.MILLISECONDS.toNanos(400));
                controlRelease.countDown();
                pendingControl.get(3, TimeUnit.SECONDS);
                assertThat(worker.isLocallyReady()).isFalse();
                worker.poll();
                worker.heartbeat();
                verify(leases, times(1)).claim(anyString(), anyLong());
                verify(leases, never()).heartbeat(any(), anyLong());
                // The stale success at 400 ms must not move the original deadline to 1200 ms.
                now.set(TimeUnit.MILLISECONDS.toNanos(1000));
                worker.checkRuntimeControl();
                assertThat(worker.isLocallyReady()).isFalse();
                verify(registry, times(alreadyHeld ? 3 : 2)).heartbeat(anyString(), anyInt());
                verify(registry, times(1)).register(anyString(), eq(1));
            } finally {
                claimRelease.countDown();
                controlRelease.countDown();
            }
        }
    }

    @Test
    void databaseHoldIncludesAClaimAtTheActiveRegistrationBoundary() throws Exception {
        var now = new java.util.concurrent.atomic.AtomicLong();
        when(registry.ttlMillis()).thenReturn(1000L);
        var admissionEntered = new CountDownLatch(1);
        var admissionRelease = new CountDownLatch(1);
        var executionRelease = new CountDownLatch(1);
        var admissionThread = new java.util.concurrent.atomic.AtomicReference<Thread>();
        var controlThread = new java.util.concurrent.atomic.AtomicReference<Thread>();
        var admissionPaused = new java.util.concurrent.atomic.AtomicBoolean();
        var command = mock(Command.class);
        when(command.attemptId())
                .thenAnswer(
                        i -> {
                            if (Thread.currentThread() == admissionThread.get()
                                    && admissionPaused.compareAndSet(false, true)) {
                                admissionEntered.countDown();
                                assertThat(admissionRelease.await(5, TimeUnit.SECONDS)).isTrue();
                            }
                            return "admission-race";
                        });
        Claim admitted = new Claim(command, claim.turn());
        when(leases.claim(anyString(), anyLong())).thenReturn(admitted, null);
        doAnswer(
                        i -> {
                            assertThat(executionRelease.await(5, TimeUnit.SECONDS)).isTrue();
                            return null;
                        })
                .when(execution)
                .execute(eq(admitted), any());
        when(registry.heartbeat(anyString(), anyInt()))
                .thenThrow(new DataAccessResourceFailureException("database unavailable"))
                .thenReturn(true);
        try (var worker = worker(now);
                var callers = Executors.newFixedThreadPool(2)) {
            try {
                var pendingClaim =
                        callers.submit(
                                () -> {
                                    admissionThread.set(Thread.currentThread());
                                    worker.poll();
                                });
                assertThat(admissionEntered.await(3, TimeUnit.SECONDS)).isTrue();
                var pendingControl =
                        callers.submit(
                                () -> {
                                    controlThread.set(Thread.currentThread());
                                    worker.checkRuntimeControl();
                                });
                // Hold and active registration share an atomic boundary: control cannot finish
                // its snapshot while this claim is paused halfway through registration.
                org.awaitility.Awaitility.await()
                        .atMost(java.time.Duration.ofSeconds(3))
                        .until(
                                () ->
                                        controlThread.get() != null
                                                && controlThread.get().getState()
                                                        == Thread.State.BLOCKED);
                assertThat(pendingControl.isDone()).isFalse();
                admissionRelease.countDown();
                pendingClaim.get(3, TimeUnit.SECONDS);
                pendingControl.get(3, TimeUnit.SECONDS);
                assertThat(worker.isLocallyReady()).isFalse();
                verify(execution).interrupt(admitted);
                worker.checkRuntimeControl();
                assertThat(worker.isLocallyReady()).isTrue();
                worker.heartbeat();
                verify(leases, never()).heartbeat(any(), anyLong());
            } finally {
                admissionRelease.countDown();
                executionRelease.countDown();
            }
        }
    }

    @Test
    void authoritativeFailureDuringInitialRegistrationIsPermanent() throws Exception {
        var now = new java.util.concurrent.atomic.AtomicLong();
        when(registry.ttlMillis()).thenReturn(1000L);
        doThrow(new Blocked("worker_unavailable")).when(registry).register(anyString(), eq(1));
        try (var worker = worker(now)) {
            worker.checkRuntimeControl();
            worker.poll();
            worker.heartbeat();
            worker.checkRuntimeControl();
            assertThat(worker.isLocallyReady()).isFalse();
            verify(registry, times(1)).register(anyString(), eq(1));
            verifyNoInteractions(leases);
        }
    }

    private static Claim nextClaim() {
        return new Claim(
                new Command("next-command", "next-turn", "running", "next-attempt", 1, true, null),
                new Turn(
                        "next-turn",
                        "v",
                        "tenant",
                        "alice",
                        "next-key",
                        "hash",
                        "input",
                        "release",
                        "running",
                        2,
                        null,
                        null));
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
