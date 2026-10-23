package com.financeclaw.worker;

import static org.awaitility.Awaitility.await;
import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

import com.financeclaw.app.execution.*;
import com.financeclaw.app.task.WorkSignals;
import com.financeclaw.app.task.WorkSignals.Queue;
import com.financeclaw.domain.Task.*;
import java.time.Duration;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicLong;
import org.junit.jupiter.api.Test;

class TaskWorkerPollingTest {
  private final ExecutionLeaseService leases = mock(ExecutionLeaseService.class);
  private final RecoverTaskService recovery = mock(RecoverTaskService.class);
  private final ExecuteTaskService execution = mock(ExecuteTaskService.class);
  private final WorkSignals signals = new WorkSignals();
  private final AtomicLong now = new AtomicLong();
  private final IdlePoll idle = new IdlePoll(250, 5000, now::get);
  private final Claim claim = claim("first");

  @Test
  void idleTicksDoNotQueryAndACommitWakesImmediately() throws Exception {
    try (var worker = worker(true, 1)) {
      worker.poll();
      for (int i = 0; i < 10; i++) worker.poll();
      verify(leases, times(1)).claim(anyString(), anyLong());
      now.addAndGet(TimeUnit.MILLISECONDS.toNanos(250));
      worker.poll();
      verify(leases, times(2)).claim(anyString(), anyLong());
      now.addAndGet(TimeUnit.MILLISECONDS.toNanos(250));
      worker.poll();
      verify(leases, times(2)).claim(anyString(), anyLong());
      signals.afterCommit(Queue.COMMAND);
      worker.poll();
      verify(leases, times(3)).claim(anyString(), anyLong());
      verifyNoInteractions(recovery, execution);
    }
  }

  @Test
  void failedClaimsBackOffWithoutLosingCapacityOrBlockingRecoveryAndCommitWakeups()
      throws Exception {
    when(leases.claim(anyString(), anyLong()))
        .thenThrow(new IllegalStateException("database unavailable"));
    try (var worker = worker(true, 1)) {
      assertThrows(IllegalStateException.class, worker::poll);
      for (int i = 0; i < 10; i++) assertDoesNotThrow(worker::poll);
      verify(leases, times(1)).claim(anyString(), anyLong());
      now.addAndGet(TimeUnit.MILLISECONDS.toNanos(250));
      assertThrows(IllegalStateException.class, worker::poll);
      now.addAndGet(TimeUnit.MILLISECONDS.toNanos(250));
      assertDoesNotThrow(worker::poll);
      verify(leases, times(2)).claim(anyString(), anyLong());
      worker.recover();
      verify(recovery).recoverExpired();
      signals.afterCommit(Queue.COMMAND);
      assertThrows(IllegalStateException.class, worker::poll);
      verify(leases, times(3)).claim(anyString(), anyLong());
      verifyNoInteractions(execution);
    }
  }

  @Test
  void generationChangeDuringAnEmptySqlQueryIsNotLostAndPollCallsCannotOverlap() throws Exception {
    var entered = new CountDownLatch(1);
    var release = new CountDownLatch(1);
    when(leases.claim(anyString(), anyLong()))
        .thenAnswer(
            invocation -> {
              entered.countDown();
              assertTrue(release.await(3, TimeUnit.SECONDS));
              return null;
            });
    try (var worker = worker(true, 1);
        var caller = Executors.newSingleThreadExecutor()) {
      var query = caller.submit(worker::poll);
      try {
        assertTrue(entered.await(3, TimeUnit.SECONDS));
        worker.poll();
        verify(leases, times(1)).claim(anyString(), anyLong());
        signals.afterCommit(Queue.COMMAND);
      } finally {
        release.countDown();
      }
      query.get(3, TimeUnit.SECONDS);
      worker.poll();
      verify(leases, times(2)).claim(anyString(), anyLong());
    }
  }

  @Test
  void releasingExecutionCapacityWakesAnAlreadyIdleScanner() throws Exception {
    var started = new CountDownLatch(1);
    var release = new CountDownLatch(1);
    when(leases.claim(anyString(), anyLong())).thenReturn(claim, null);
    doAnswer(
            invocation -> {
              started.countDown();
              assertTrue(release.await(3, TimeUnit.SECONDS));
              return null;
            })
        .when(execution)
        .execute(claim);
    try (var worker = worker(true, 2)) {
      try {
        worker.poll(); // Claim one task, then see no additional work and enter backoff.
        assertTrue(started.await(3, TimeUnit.SECONDS));
        verify(leases, times(2)).claim(anyString(), anyLong());
        worker.poll();
        verify(leases, times(2)).claim(anyString(), anyLong());
      } finally {
        release.countDown();
      }
      // The clock remains frozen. Only releasing capacity can make another scan eligible.
      await()
          .atMost(Duration.ofSeconds(3))
          .pollInterval(Duration.ofMillis(5))
          .untilAsserted(
              () -> {
                worker.poll();
                verify(leases, times(3)).claim(anyString(), anyLong());
              });
    }
  }

  @Test
  void fullExecutionCapacityDoesNotPreventRecoveryOrHeartbeat() throws Exception {
    var started = new CountDownLatch(1);
    var release = new CountDownLatch(1);
    when(leases.claim(anyString(), anyLong())).thenReturn(claim);
    doAnswer(
            invocation -> {
              started.countDown();
              assertTrue(release.await(3, TimeUnit.SECONDS));
              return null;
            })
        .when(execution)
        .execute(claim);
    try (var worker = worker(true, 1)) {
      try {
        worker.poll();
        assertTrue(started.await(3, TimeUnit.SECONDS));
        worker.poll();
        worker.recover();
        worker.heartbeat();
        worker.recover();
        verify(leases, times(1)).claim(anyString(), anyLong());
        await()
            .atMost(Duration.ofSeconds(3))
            .untilAsserted(() -> verify(leases).heartbeat(claim, 15000));
        verify(recovery, times(2)).recoverExpired();
      } finally {
        release.countDown();
      }
    }
  }

  @Test
  void aBlockedHeartbeatDoesNotDelayOtherAttemptsOrOverlapOnRepeatedTicks() throws Exception {
    var other = claim("second");
    var started = new CountDownLatch(2);
    var releaseExecution = new CountDownLatch(1);
    var blockedHeartbeat = new CountDownLatch(1);
    var releaseHeartbeat = new CountDownLatch(1);
    var heartbeatFinished = new CountDownLatch(1);
    var otherRenewed = new CountDownLatch(1);
    when(leases.claim(anyString(), anyLong())).thenReturn(claim, other);
    doAnswer(
            invocation -> {
              started.countDown();
              assertTrue(releaseExecution.await(3, TimeUnit.SECONDS));
              return null;
            })
        .when(execution)
        .execute(any());
    when(leases.heartbeat(claim, 15000))
        .thenAnswer(
            invocation -> {
              blockedHeartbeat.countDown();
              try {
                assertTrue(releaseHeartbeat.await(3, TimeUnit.SECONDS));
                return false;
              } finally {
                heartbeatFinished.countDown();
              }
            });
    when(leases.heartbeat(other, 15000))
        .thenAnswer(
            invocation -> {
              otherRenewed.countDown();
              return false;
            });
    try (var worker = worker(true, 2)) {
      try {
        worker.poll();
        assertTrue(started.await(3, TimeUnit.SECONDS));
        worker.heartbeat();
        assertTrue(blockedHeartbeat.await(3, TimeUnit.SECONDS));
        assertTrue(otherRenewed.await(3, TimeUnit.SECONDS));
        for (int i = 0; i < 20; i++) worker.heartbeat();
        verify(leases, times(1)).heartbeat(claim, 15000);
        verify(leases, atLeastOnce()).heartbeat(other, 15000);
      } finally {
        releaseHeartbeat.countDown();
        releaseExecution.countDown();
        assertTrue(heartbeatFinished.await(3, TimeUnit.SECONDS));
      }
    }
  }

  @Test
  void closingInterruptsInFlightHeartbeatsAndPreventsNewRenewals() throws Exception {
    var started = new CountDownLatch(1);
    var releaseExecution = new CountDownLatch(1);
    var heartbeatStarted = new CountDownLatch(1);
    var heartbeatInterrupted = new CountDownLatch(1);
    when(leases.claim(anyString(), anyLong())).thenReturn(claim);
    doAnswer(
            invocation -> {
              started.countDown();
              assertTrue(releaseExecution.await(3, TimeUnit.SECONDS));
              return null;
            })
        .when(execution)
        .execute(claim);
    doAnswer(
            invocation -> {
              releaseExecution.countDown();
              return null;
            })
        .when(execution)
        .interrupt(claim);
    when(leases.heartbeat(claim, 15000))
        .thenAnswer(
            invocation -> {
              heartbeatStarted.countDown();
              try {
                new CountDownLatch(1).await(3, TimeUnit.SECONDS);
              } catch (InterruptedException interrupted) {
                heartbeatInterrupted.countDown();
                Thread.currentThread().interrupt();
              }
              return false;
            });
    try (var worker = worker(true, 1)) {
      worker.poll();
      assertTrue(started.await(3, TimeUnit.SECONDS));
      worker.heartbeat();
      assertTrue(heartbeatStarted.await(3, TimeUnit.SECONDS));
      worker.close();
      assertTrue(heartbeatInterrupted.await(3, TimeUnit.SECONDS));
      for (int i = 0; i < 10; i++) worker.heartbeat();
      verify(leases, times(1)).heartbeat(claim, 15000);
    }
  }

  @Test
  void recoveryRunsDespiteCommandBackoffAndDoesNotRenewExpiredLeases() throws Exception {
    try (var worker = worker(true, 1)) {
      worker.poll();
      worker.poll();
      worker.recover();
      verify(leases, times(1)).claim(anyString(), anyLong());
      verify(recovery).recoverExpired();
      verify(leases, never()).heartbeat(any(), anyLong());
      verifyNoInteractions(execution);
    }
  }

  @Test
  void disabledAndClosedWorkersDoNotTouchTheDatabase() throws Exception {
    try (var disabled = worker(false, 1)) {
      disabled.poll();
      disabled.recover();
      disabled.heartbeat();
      verifyNoInteractions(leases, recovery, execution);
    }
    var closed = worker(true, 1);
    closed.close();
    signals.afterCommit(Queue.COMMAND);
    closed.poll();
    closed.recover();
    closed.heartbeat();
    verifyNoInteractions(leases, recovery, execution);
  }

  private TaskWorker worker(boolean enabled, int parallelism) {
    return new TaskWorker(
        leases, recovery, execution, signals, enabled, parallelism, 15000, 2000, idle);
  }

  private static Claim claim(String suffix) {
    return new Claim(
        new Command(
            "command-" + suffix, "turn-" + suffix, "running", "attempt-" + suffix, 1, true, null),
        new Turn(
            "turn-" + suffix,
            "conversation-" + suffix,
            "tenant",
            "alice",
            "key-" + suffix,
            "hash",
            "message",
            "release",
            "running",
            2,
            null,
            null));
  }
}
