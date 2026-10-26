package com.financeclaw.worker;

import com.financeclaw.app.execution.*;
import com.financeclaw.app.runtime.WorkerRegistry;
import com.financeclaw.app.task.WorkSignals;
import com.financeclaw.app.task.WorkSignals.Queue;
import com.financeclaw.domain.Task.*;
import jakarta.annotation.PreDestroy;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicBoolean;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.dao.DataAccessException;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;
import org.springframework.transaction.CannotCreateTransactionException;

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
    private final WorkerRegistry registry;
    private final ExecutionPolicyService policy;
    private final int parallelism;
    private final java.util.function.LongSupplier monotonic;
    private final long registryTtlNanos;
    private volatile long registryDeadlineNanos;
    private final Set<String> denied = ConcurrentHashMap.newKeySet();
    private volatile boolean registered;
    private volatile boolean unavailable;
    private volatile boolean controlHeld;
    private volatile long availabilityGeneration;
    private final Object availabilityLock = new Object();
    private final AtomicBoolean controlling = new AtomicBoolean();

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
            @Value("${financeclaw.worker.idle-max-ms:5000}") long idleMaximumMillis,
            WorkerRegistry registry,
            ExecutionPolicyService policy) {
        this(
                leases,
                recovery,
                execution,
                signals,
                enabled,
                parallelism,
                lease,
                heartbeat,
                new IdlePoll(pollMillis, idleMaximumMillis),
                registry,
                policy);
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
        this(
                leases,
                recovery,
                execution,
                signals,
                enabled,
                parallelism,
                lease,
                heartbeat,
                idle,
                null,
                null);
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
            IdlePoll idle,
            WorkerRegistry registry,
            ExecutionPolicyService policy) {
        this(
                leases,
                recovery,
                execution,
                signals,
                enabled,
                parallelism,
                lease,
                heartbeat,
                idle,
                registry,
                policy,
                System::nanoTime);
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
            IdlePoll idle,
            WorkerRegistry registry,
            ExecutionPolicyService policy,
            java.util.function.LongSupplier monotonic) {
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
        this.registry = registry;
        this.policy = policy;
        this.parallelism = parallelism;
        this.monotonic = java.util.Objects.requireNonNull(monotonic);
        this.registryTtlNanos =
                registry == null ? 0 : TimeUnit.MILLISECONDS.toNanos(registry.ttlMillis());
        if (registry != null
                && (registryTtlNanos < TimeUnit.SECONDS.toNanos(1)
                        || registryTtlNanos > TimeUnit.SECONDS.toNanos(300)))
            throw new IllegalArgumentException("Invalid registry lifetime");
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
        if (closing || !enabled || !locallyEligible() || !polling.compareAndSet(false, true)) {
            return;
        }
        try {
            ensureRegistered();
            if (!locallyEligible() || slots.availablePermits() == 0) {
                return;
            }
            // 先检查容量，再观察提交代数；没有空槽时连领取 SQL 都不执行。
            var ticket = idle.acquire(signals.generation(Queue.COMMAND));
            if (ticket == null) {
                return;
            }
            // 槽位覆盖领取到执行结束的全过程；异常和线程池拒绝也必须归还。
            while (!closing && locallyEligible() && slots.tryAcquire()) {
                long generation;
                synchronized (availabilityLock) {
                    if (!isLocallyReady()) {
                        slots.release();
                        break;
                    }
                    generation = availabilityGeneration;
                }
                Claim claim;
                try {
                    claim = leases.claim(worker, lease);
                } catch (RuntimeException failure) {
                    slots.release();
                    idle.empty(ticket);
                    throw failure;
                }
                if (claim == null || !admit(claim, generation)) {
                    slots.release();
                    idle.empty(ticket);
                    break;
                }
                idle.worked();
                try {
                    pool.execute(
                            () -> {
                                try {
                                    if (!closing
                                            && locallyEligible()
                                            && generation == availabilityGeneration
                                            && !denied.contains(claim.command().attemptId()))
                                        execution.execute(
                                                claim,
                                                () ->
                                                        !closing
                                                                && isLocallyReady()
                                                                && generation
                                                                        == availabilityGeneration
                                                                && !denied.contains(
                                                                        claim.command().attemptId())
                                                                && active.get(
                                                                                claim.command()
                                                                                        .attemptId())
                                                                        == claim);
                                } finally {
                                    release(claim);
                                    slots.release();
                                    idle.wake();
                                }
                            });
                } catch (RejectedExecutionException failure) {
                    release(claim);
                    slots.release();
                    idle.wake();
                    // 已领取但未执行的命令交给租约恢复；此处不能伪造失败结果或直接重新入队。
                    break;
                }
            }
        } catch (DataAccessException | CannotCreateTransactionException unavailableDatabase) {
            holdControl();
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

    private boolean admit(Claim claim, long generation) {
        if (!locallyEligible()) return false;
        synchronized (availabilityLock) {
            if (!isLocallyReady() || generation != availabilityGeneration) return false;
            // Hold/loss marks the same active set under this lock, before any recovery can admit
            // new work. A claim cannot slip between that snapshot and its generation check.
            active.put(claim.command().attemptId(), claim);
            return true;
        }
    }

    private void release(Claim claim) {
        synchronized (availabilityLock) {
            active.remove(claim.command().attemptId(), claim);
            denied.remove(claim.command().attemptId());
        }
    }

    /** 把活跃 attempt 的续期派发到独立线程池。租约丢失或取消时中断执行，不根据本地时间续写结果。 */
    @Scheduled(
            fixedDelayString = "${financeclaw.worker.heartbeat-ms:2000}",
            scheduler = "executionHeartbeatScheduler")
    public void heartbeat() {
        if (closing || !enabled || !locallyEligible()) {
            return;
        }
        // 同一 attempt 同时最多一个续期；线程池不排队，避免旧心跳在任务结束后积压执行。
        for (var claim : active.values()) {
            if (closing || !locallyEligible()) {
                return;
            }
            var attempt = claim.command().attemptId();
            if (denied.contains(attempt)) continue;
            if (!renewing.add(attempt)) {
                continue;
            }
            try {
                heartbeatPool.execute(
                        () -> {
                            try {
                                if (closing
                                        || !locallyEligible()
                                        || denied.contains(attempt)
                                        || active.get(attempt) != claim) {
                                    return;
                                }
                                try {
                                    boolean cancelling = leases.heartbeat(claim, lease);
                                    if (!locallyEligible()) return;
                                    if (cancelling) execution.interrupt(claim);
                                } catch (RuntimeException lost) {
                                    denied.add(attempt);
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

    /** 控制面检查独立于专池续租，避免业务池占满时先排队借业务连接再续租。 */
    @Scheduled(fixedDelayString = "${financeclaw.runtime.control-poll-ms:1000}")
    public void checkRuntimeControl() {
        // A temporary database hold stops work, but must not stop the control retry that clears it.
        if (closing || !enabled || !registrationLive() || !controlling.compareAndSet(false, true))
            return;
        try {
            try {
                ensureRegistered();
                if (!registrationLive()) return;
                boolean releasedHold;
                if (registry != null) {
                    long previousDeadline;
                    long generation;
                    long started;
                    synchronized (availabilityLock) {
                        previousDeadline = registryDeadlineNanos;
                        generation = availabilityGeneration;
                        started = monotonic.getAsLong();
                    }
                    if (!registry.heartbeat(worker, active.size())) {
                        loseRegistration();
                        return;
                    }
                    // Compare the old deadline before publishing any refreshed one. Even without
                    // another scheduler tick, a late database success cannot revive this UUID.
                    boolean timely;
                    boolean stale;
                    synchronized (availabilityLock) {
                        long now = monotonic.getAsLong();
                        timely =
                                !unavailable
                                        && now - previousDeadline < 0
                                        && now - (started + registryTtlNanos) < 0;
                        stale = generation != availabilityGeneration;
                        releasedHold = controlHeld;
                        if (timely && !stale) {
                            registryDeadlineNanos = started + registryTtlNanos;
                            controlHeld = false;
                        }
                    }
                    if (!timely) {
                        loseRegistration();
                        return;
                    }
                    // An error observed after this request began needs a fresh control read.
                    // Neither the hold nor its old deadline can be overwritten by this response.
                    if (stale) return;
                } else {
                    releasedHold = controlHeld;
                    controlHeld = false;
                }
                if (releasedHold) idle.wake();
            } catch (DataAccessException | CannotCreateTransactionException unavailableDatabase) {
                holdControl();
                return;
            } catch (RuntimeException lost) {
                loseRegistration();
                return;
            }
            if (policy != null)
                for (var claim : active.values()) {
                    if (closing || !locallyEligible()) return;
                    if (denied.contains(claim.command().attemptId())) continue;
                    try {
                        policy.checkDispatch(claim);
                    } catch (DataAccessException
                            | CannotCreateTransactionException unavailableDatabase) {
                        holdControl();
                        return;
                    } catch (RuntimeException revoked) {
                        denied.add(claim.command().attemptId());
                        execution.interrupt(claim);
                    }
                }
        } finally {
            controlling.set(false);
        }
    }

    private synchronized void ensureRegistered() {
        if (registry != null && !registered && !unavailable) {
            long started = monotonic.getAsLong();
            try {
                registry.register(worker, parallelism);
            } catch (DataAccessException | CannotCreateTransactionException unavailableDatabase) {
                throw unavailableDatabase;
            } catch (RuntimeException lost) {
                loseRegistration();
                throw lost;
            }
            registryDeadlineNanos = started + registryTtlNanos;
            registered = true;
        }
    }

    /** 专用心跳只读本机单调时间，不借业务池；控制调度饥饿时也会保守停止续租。 */
    private boolean locallyEligible() {
        return registrationLive() && !controlHeld;
    }

    private boolean registrationLive() {
        if (unavailable) return false;
        if (registry != null && registered && monotonic.getAsLong() - registryDeadlineNanos >= 0) {
            loseRegistration();
            return false;
        }
        return true;
    }

    /**
     * Readiness only observes local control state; it does not borrow a connection or interrupt.
     */
    public boolean isLocallyReady() {
        if (closing) return false;
        if (!enabled) return true;
        return !unavailable
                && !controlHeld
                && (registry == null
                        || (registered && monotonic.getAsLong() - registryDeadlineNanos < 0));
    }

    private void holdControl() {
        if (!registrationLive()) return;
        List<Claim> interrupted;
        synchronized (availabilityLock) {
            if (unavailable) return;
            controlHeld = true;
            availabilityGeneration++;
            interrupted = markActiveDenied();
        }
        interrupted.forEach(execution::interrupt);
    }

    private void loseRegistration() {
        List<Claim> interrupted;
        synchronized (availabilityLock) {
            if (unavailable) return;
            unavailable = true;
            availabilityGeneration++;
            interrupted = markActiveDenied();
        }
        interrupted.forEach(execution::interrupt);
        // 不再续租或重新登记这个 UUID；原租约过期后由既有 receipt/checkpoint recovery 收尾。
    }

    /** Called only while availabilityLock protects admission and control state. */
    private List<Claim> markActiveDenied() {
        return active.values().stream()
                .filter(claim -> denied.add(claim.command().attemptId()))
                .toList();
    }

    /** 先停止新工作和续期，再中断执行并等待线程退出；未封存结果由持久恢复链路接管。 */
    @Override
    @PreDestroy
    public void close() throws InterruptedException {
        closing = true;
        if (registry != null && registered) {
            try {
                registry.stop(worker);
            } catch (RuntimeException ignored) {
                /* DB expiry fences it. */
            }
        }
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
