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

/**
 * 将持久命令领取为有租约的执行尝试，并独立维护恢复与执行心跳。
 *
 * <p>信号和退避只减少空查询；SQL command fence 始终决定谁可以提交。执行槽位在 领取前预留，避免把排队时间算进租约。心跳使用无积压的独立线程池，慢模型调用不会
 * 阻塞所有任务续期；取消、失去租约或撤权后中断对应 attempt。
 */
@Component
public class TaskWorker implements AutoCloseable {
    private static final int MAX_PARALLELISM = 32;
    private static final long MIN_HEARTBEAT_MILLIS = 100;
    private static final int MIN_LEASE_HEARTBEAT_RATIO = 3;
    private static final int SHUTDOWN_TIMEOUT_SECONDS = 5;
    private static final long DEFAULT_POLL_MILLIS = 250;
    private static final long DEFAULT_IDLE_MAX_MILLIS = 5000;
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
                new IdlePoll(DEFAULT_POLL_MILLIS, DEFAULT_IDLE_MAX_MILLIS));
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
        if (parallelism < 1
                || parallelism > MAX_PARALLELISM
                || lease < heartbeat * MIN_LEASE_HEARTBEAT_RATIO
                || heartbeat < MIN_HEARTBEAT_MILLIS) {
            throw new IllegalArgumentException("Invalid worker limits");
        }
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
        pool.setAwaitTerminationSeconds(SHUTDOWN_TIMEOUT_SECONDS);
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

    /** 扫描可领取命令并提交到有界执行池。进程内标记只防止同实例重入，跨实例竞争由 SQL 行锁处理。 */
    @Scheduled(fixedDelayString = "${financeclaw.worker.poll-ms:250}")
    public void poll() {
        if (closing || !enabled || !polling.compareAndSet(false, true)) {
            return;
        }
        try {
            if (slots.availablePermits() == 0) {
                return;
            }
            // 先检查容量，再观察提交代数；没有空槽时连领取 SQL 都不执行。
            var ticket = idle.acquire(signals.generation(Queue.COMMAND));
            if (ticket == null) {
                return;
            }
            // 槽位覆盖领取到执行结束的全过程；异常和线程池拒绝也必须归还。
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
                // 在提交执行线程前登记 attempt，让首次心跳也能覆盖尚未开始运行的任务。
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
                    // 已领取但未执行的命令交给租约恢复；此处不能伪造失败结果或直接重新入队。
                    break;
                }
            }
        } finally {
            polling.set(false);
        }
    }

    /** Recovery must run even when command polling is idle or every execution slot is occupied. */
    /** 独立恢复已过期 command；即使领取退避或执行槽位占满也要继续检查失联任务。 */
    @Scheduled(fixedDelayString = "${financeclaw.worker.recovery-ms:2000}")
    public void recover() {
        if (closing || !enabled || !recovering.compareAndSet(false, true)) {
            return;
        }
        try {
            recovery.recoverExpired();
        } finally {
            recovering.set(false);
        }
    }

    public void execute(Claim claim) {
        execution.execute(claim);
    }

    /** 把活跃 attempt 的续期派发到独立线程池。租约丢失或取消时中断执行，不根据本地时间续写结果。 */
    @Scheduled(
            fixedDelayString = "${financeclaw.worker.heartbeat-ms:2000}",
            scheduler = "executionHeartbeatScheduler")
    public void heartbeat() {
        if (closing || !enabled) {
            return;
        }
        // 同一 attempt 同时最多一个续期；线程池不排队，避免旧心跳在任务结束后积压执行。
        for (var claim : active.values()) {
            if (closing) {
                return;
            }
            var attempt = claim.command().attemptId();
            if (!renewing.add(attempt)) {
                continue;
            }
            try {
                heartbeatPool.execute(
                        () -> {
                            try {
                                if (closing || active.get(attempt) != claim) {
                                    return;
                                }
                                try {
                                    if (leases.heartbeat(claim, lease)) {
                                        execution.interrupt(claim);
                                    }
                                } catch (RuntimeException lost) {
                                    execution.interrupt(claim);
                                }
                            } finally {
                                renewing.remove(attempt);
                            }
                        });
            } catch (RejectedExecutionException rejected) {
                // 不积压续期请求：下一调度周期重试，租约是否仍有效始终由 SQL 判定。
                renewing.remove(attempt);
            }
        }
    }

    /** 先停止新工作和续期，再中断执行并等待线程退出；未封存结果由持久恢复链路接管。 */
    @Override
    @PreDestroy
    public void close() throws InterruptedException {
        closing = true;
        heartbeatPool.shutdownNow();
        renewing.clear();
        active.values().forEach(execution::interrupt);
        pool.shutdown();
        if (!pool.getThreadPoolExecutor().isTerminated()) {
            pool.getThreadPoolExecutor().shutdownNow();
        }
        heartbeatPool.awaitTermination(SHUTDOWN_TIMEOUT_SECONDS, TimeUnit.SECONDS);
    }
}
