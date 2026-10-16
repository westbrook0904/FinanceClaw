package com.financeclaw.worker;

import com.financeclaw.app.execution.*;
import com.financeclaw.app.task.ProjectTaskService;
import com.financeclaw.domain.Task.*;
import jakarta.annotation.PreDestroy;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.*;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

@Component
public class TaskWorker implements AutoCloseable {
  private final ExecutionLeaseService leases;
  private final RecoverTaskService recovery;
  private final ProjectTaskService projection;
  private final ExecuteTaskService execution;
  private final boolean enabled, integrations;
  private final long lease;
  private final Semaphore slots;
  private final org.springframework.scheduling.concurrent.ThreadPoolTaskExecutor pool;
  private final Map<String, Claim> active = new ConcurrentHashMap<>();
  private final String worker = UUID.randomUUID().toString();
  private volatile boolean closing;

  public TaskWorker(
      ExecutionLeaseService leases,
      RecoverTaskService recovery,
      ProjectTaskService projection,
      ExecuteTaskService execution,
      @Value("${financeclaw.worker.enabled:true}") boolean enabled,
      @Value("${financeclaw.integrations.enabled:true}") boolean integrations,
      @Value("${financeclaw.worker.parallelism:4}") int parallelism,
      @Value("${financeclaw.worker.lease-ms:15000}") long lease,
      @Value("${financeclaw.worker.heartbeat-ms:2000}") long heartbeat) {
    if (parallelism < 1 || parallelism > 32 || lease < heartbeat * 3 || heartbeat < 100)
      throw new IllegalArgumentException("Invalid worker limits");
    this.leases = leases;
    this.recovery = recovery;
    this.projection = projection;
    this.execution = execution;
    this.enabled = enabled;
    this.integrations = integrations;
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
  }

  @Scheduled(fixedDelayString = "${financeclaw.worker.poll-ms:250}")
  public void poll() {
    if (closing) return;
    if (integrations)
      for (int i = 0; i < 50 && projection.projectNext(); i++) {
        /* bounded local outbox drain */
      }
    if (!enabled) return;
    recovery.recoverExpired();
    while (!closing && slots.tryAcquire()) {
      Claim c;
      try {
        c = leases.claim(worker, lease);
      } catch (RuntimeException e) {
        slots.release();
        throw e;
      }
      if (c == null) {
        slots.release();
        break;
      }
      active.put(c.command().attemptId(), c);
      try {
        pool.execute(
            () -> {
              try {
                execute(c);
              } finally {
                active.remove(c.command().attemptId());
                slots.release();
              }
            });
      } catch (RejectedExecutionException e) {
        active.remove(c.command().attemptId());
        slots.release();
      }
    }
  }

  public void execute(Claim claim) {
    execution.execute(claim);
  }

  @Scheduled(fixedDelayString = "${financeclaw.worker.heartbeat-ms:2000}")
  public void heartbeat() {
    active
        .values()
        .forEach(
            c -> {
              try {
                if (leases.heartbeat(c, lease)) execution.interrupt(c);
              } catch (RuntimeException lost) {
                execution.interrupt(c);
              }
            });
  }

  @PreDestroy
  public void close() throws InterruptedException {
    closing = true;
    active.values().forEach(execution::interrupt);
    pool.shutdown();
    if (!pool.getThreadPoolExecutor().isTerminated()) pool.getThreadPoolExecutor().shutdownNow();
  }
}
