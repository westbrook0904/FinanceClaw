package com.financeclaw.worker;

import com.financeclaw.app.execution.*;
import com.financeclaw.app.task.WorkSignals;
import com.financeclaw.app.task.WorkSignals.Queue;
import com.financeclaw.domain.Task.*;
import jakarta.annotation.PreDestroy;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicBoolean;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

@Component
public class TaskWorker implements AutoCloseable {
  private final ExecutionLeaseService leases;
  private final RecoverTaskService recovery;
  private final ExecuteTaskService execution;
  private final boolean enabled;
  private final WorkSignals signals;
  private final IdlePoll idle;
  private final AtomicBoolean polling = new AtomicBoolean();
  private final AtomicBoolean recovering = new AtomicBoolean();
  private final long lease;
  private final Semaphore slots;
  private final org.springframework.scheduling.concurrent.ThreadPoolTaskExecutor pool;
  private final ExecutorService heartbeatPool;
  private final Map<String, Claim> active = new ConcurrentHashMap<>();
  private final Set<String> renewing = ConcurrentHashMap.newKeySet();
  private final String worker = UUID.randomUUID().toString();
  private volatile boolean closing;

  @Autowired
  public TaskWorker(
      ExecutionLeaseService leases,
      RecoverTaskService recovery,
      ExecuteTaskService execution,
      WorkSignals signals,
      @Value("${financeclaw.worker.enabled:true}") boolean enabled,
      @Value("${financeclaw.worker.parallelism:4}") int parallelism,
      @Value("${financeclaw.worker.lease-ms:15000}") long lease,
      @Value("${financeclaw.worker.heartbeat-ms:2000}") long heartbeat,
      @Value("${financeclaw.worker.poll-ms:250}") long pollMillis,
      @Value("${financeclaw.worker.idle-max-ms:5000}") long idleMaximumMillis) {
    this(
        leases,
        recovery,
        execution,
        signals,
        enabled,
        parallelism,
        lease,
        heartbeat,
        new IdlePoll(pollMillis, idleMaximumMillis));
  }

  public TaskWorker(
      ExecutionLeaseService leases,
      RecoverTaskService recovery,
      ExecuteTaskService execution,
      WorkSignals signals,
      boolean enabled,
      int parallelism,
      long lease,
      long heartbeat) {
    this(
        leases,
        recovery,
        execution,
        signals,
        enabled,
        parallelism,
        lease,
        heartbeat,
        new IdlePoll(250, 5000));
  }

  TaskWorker(
      ExecutionLeaseService leases,
      RecoverTaskService recovery,
      ExecuteTaskService execution,
      WorkSignals signals,
      boolean enabled,
      int parallelism,
      long lease,
      long heartbeat,
      IdlePoll idle) {
    if (parallelism < 1 || parallelism > 32 || lease < heartbeat * 3 || heartbeat < 100)
      throw new IllegalArgumentException("Invalid worker limits");
    this.leases = leases;
    this.recovery = recovery;
    this.execution = execution;
    this.enabled = enabled;
    this.signals = java.util.Objects.requireNonNull(signals);
    this.idle = java.util.Objects.requireNonNull(idle);
    this.lease = lease;
    slots = new Semaphore(parallelism);
    pool = new org.springframework.scheduling.concurrent.ThreadPoolTaskExecutor();
    pool.setCorePoolSize(parallelism);
    pool.setMaxPoolSize(parallelism);
    pool.setQueueCapacity(parallelism);
    pool.setThreadNamePrefix("task-worker-");
    pool.setWaitForTasksToCompleteOnShutdown(true);
    pool.setAwaitTerminationSeconds(5);
    pool.initialize();
    heartbeatPool =
        new ThreadPoolExecutor(
            parallelism,
            parallelism,
            0,
            TimeUnit.MILLISECONDS,
            new SynchronousQueue<>(),
            Thread.ofPlatform().name("task-heartbeat-", 0).factory(),
            new ThreadPoolExecutor.AbortPolicy());
  }

  @Scheduled(fixedDelayString = "${financeclaw.worker.poll-ms:250}")
  public void poll() {
    if (closing || !enabled || !polling.compareAndSet(false, true)) return;
    try {
      if (slots.availablePermits() == 0) return;
      var ticket = idle.acquire(signals.generation(Queue.COMMAND));
      if (ticket == null) return;
      while (!closing && slots.tryAcquire()) {
        Claim claim;
        try {
          claim = leases.claim(worker, lease);
        } catch (RuntimeException failure) {
          slots.release();
          idle.empty(ticket);
          throw failure;
        }
        if (claim == null) {
          slots.release();
          idle.empty(ticket);
          break;
        }
        idle.worked();
        active.put(claim.command().attemptId(), claim);
        try {
          pool.execute(
              () -> {
                try {
                  execute(claim);
                } finally {
                  active.remove(claim.command().attemptId());
                  slots.release();
                  idle.wake();
                }
              });
        } catch (RejectedExecutionException failure) {
          active.remove(claim.command().attemptId());
          slots.release();
          idle.wake();
          // A claim already exists; lease recovery owns reconciliation if shutdown rejected it.
          break;
        }
      }
    } finally {
      polling.set(false);
    }
  }

  /** Recovery must run even when command polling is idle or every execution slot is occupied. */
  @Scheduled(fixedDelayString = "${financeclaw.worker.recovery-ms:2000}")
  public void recover() {
    if (closing || !enabled || !recovering.compareAndSet(false, true)) return;
    try {
      recovery.recoverExpired();
    } finally {
      recovering.set(false);
    }
  }

  public void execute(Claim claim) {
    execution.execute(claim);
  }

  @Scheduled(
      fixedDelayString = "${financeclaw.worker.heartbeat-ms:2000}",
      scheduler = "executionHeartbeatScheduler")
  public void heartbeat() {
    if (closing || !enabled) return;
    for (var claim : active.values()) {
      if (closing) return;
      var attempt = claim.command().attemptId();
      if (!renewing.add(attempt)) continue;
      try {
        heartbeatPool.execute(
            () -> {
              try {
                if (closing || active.get(attempt) != claim) return;
                try {
                  if (leases.heartbeat(claim, lease)) execution.interrupt(claim);
                } catch (RuntimeException lost) {
                  execution.interrupt(claim);
                }
              } finally {
                renewing.remove(attempt);
              }
            });
      } catch (RejectedExecutionException rejected) {
        // No heartbeat backlog: an occupied executor retries on the next scheduler tick.
        renewing.remove(attempt);
      }
    }
  }

  @PreDestroy
  public void close() throws InterruptedException {
    closing = true;
    heartbeatPool.shutdownNow();
    renewing.clear();
    active.values().forEach(execution::interrupt);
    pool.shutdown();
    if (!pool.getThreadPoolExecutor().isTerminated()) pool.getThreadPoolExecutor().shutdownNow();
    heartbeatPool.awaitTermination(5, TimeUnit.SECONDS);
  }
}
