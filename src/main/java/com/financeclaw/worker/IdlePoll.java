package com.financeclaw.worker;

import java.util.concurrent.TimeUnit;
import java.util.function.LongSupplier;

/**
 * 基于单调时钟的本地空轮询退避，不承载数据库工作队列的正确性。
 *
 * <p>查询前捕获 generation/wakeVersion 票据，空结果只能延迟同一票据对应的下一次 扫描。查询期间若有提交或槽位释放，旧空结果不得覆盖新的唤醒。使用 nanoTime 差值
 * 比较截止时间，避免系统时间校准影响退避；所有状态在同一实例锁内更新。
 */
public final class IdlePoll {
    private final long initialDelay;
    private final long maximumDelay;
    private final LongSupplier clock;
    private long nextDelay;
    private long deadline;
    private long observedGeneration;
    private long wakeVersion;
    private boolean observed;
    private boolean waiting;

    public IdlePoll(long initialMillis, long maximumMillis) {
        this(initialMillis, maximumMillis, System::nanoTime);
    }

    IdlePoll(long initialMillis, long maximumMillis, LongSupplier clock) {
        if (initialMillis < 1
                || maximumMillis < initialMillis
                || maximumMillis > TimeUnit.DAYS.toMillis(1)) {
            throw new IllegalArgumentException("Invalid idle polling limits");
        }
        initialDelay = TimeUnit.MILLISECONDS.toNanos(initialMillis);
        maximumDelay = TimeUnit.MILLISECONDS.toNanos(maximumMillis);
        this.clock = java.util.Objects.requireNonNull(clock);
        nextDelay = initialDelay;
    }

    /** Capture this ticket before querying; pass that exact ticket back when the query is empty. */
    /**
     * 在查询前取得本次观察票据；新的提交代数会立即清除退避。
     *
     * @return null 表示仍处退避期，调用方应直接跳过数据库扫描
     */
    public synchronized Ticket acquire(long generation) {
        if (!observed || generation != observedGeneration) {
            observed = true;
            observedGeneration = generation;
            reset();
        }
        if (waiting && clock.getAsLong() - deadline < 0) {
            return null;
        }
        return new Ticket(observedGeneration, wakeVersion);
    }

    /** 仅当票据仍然有效时延迟下一次扫描，并将空闲间隔翻倍至上限。 旧查询结果不能覆盖查询期间发生的提交或容量唤醒。 */
    public synchronized void empty(Ticket ticket) {
        if (ticket.generation() != observedGeneration || ticket.wakeVersion() != wakeVersion) {
            return;
        }
        deadline = clock.getAsLong() + nextDelay;
        waiting = true;
        nextDelay = Math.min(maximumDelay, nextDelay * 2);
    }

    /**
     * Real work starts the next idle period at the initial delay, without invalidating its ticket.
     */
    /** 发现真实工作后恢复初始间隔；保留票据版本，允许同次扫描末尾的空结果建立短暂退避。 */
    public synchronized void worked() {
        reset();
    }

    /** Capacity becoming available permits a new scan on the next local scheduling tick. */
    /** 容量释放时推进唤醒版本；让已在途的空扫描结果失效，下一调度周期即可重新领取。 */
    public synchronized void wake() {
        wakeVersion++;
        reset();
    }

    private void reset() {
        waiting = false;
        nextDelay = initialDelay;
    }

    public record Ticket(long generation, long wakeVersion) {}
}
