package com.financeclaw.app.task;

import java.util.Set;
import java.util.concurrent.atomic.AtomicLong;
import org.springframework.stereotype.Component;
import org.springframework.transaction.support.TransactionSynchronization;
import org.springframework.transaction.support.TransactionSynchronizationManager;

/** 进程内工作唤醒信号：只保留单调代次作为轮询提示，数据库中的持久命令和通知仍是恢复及并发竞争的唯一依据。 */
@Component
public final class WorkSignals {
    public enum Queue {
        COMMAND,
        NOTICE
    }

    private static final Set<String> NOTICE_STATUSES =
            Set.of("waiting", "blocked", "completed", "failed", "cancelled");

    private final AtomicLong commands = new AtomicLong();
    private final AtomicLong notices = new AtomicLong();

    public long generation(Queue queue) {
        return counter(queue).get();
    }

    /** 只为需要渠道投影的持久任务状态唤醒通知扫描，运行增量不产生数据库工作。 */
    public void afterTaskCommit(String status) {
        if (status != null && NOTICE_STATUSES.contains(status)) {
            afterCommit(Queue.NOTICE);
        }
    }

    public void afterCommit(Queue queue) {
        var counter = counter(queue);
        if (!TransactionSynchronizationManager.isActualTransactionActive()) {
            counter.incrementAndGet();
            return;
        }
        TransactionSynchronizationManager.registerSynchronization(
                new TransactionSynchronization() {
                    @Override
                    public void afterCommit() {
                        counter.incrementAndGet();
                    }
                });
    }

    private AtomicLong counter(Queue queue) {
        return switch (queue) {
            case COMMAND -> commands;
            case NOTICE -> notices;
        };
    }
}
