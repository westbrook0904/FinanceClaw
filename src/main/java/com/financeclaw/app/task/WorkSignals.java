package com.financeclaw.app.task;

import java.util.concurrent.atomic.AtomicLong;
import org.springframework.stereotype.Component;
import org.springframework.transaction.support.TransactionSynchronization;
import org.springframework.transaction.support.TransactionSynchronizationManager;

/** 进程内工作唤醒信号：只保留单调代次作为轮询提示，数据库中的持久命令仍是恢复及并发竞争的唯一依据。 */
@Component
public final class WorkSignals {
    public enum Queue {
        COMMAND
    }

    private final AtomicLong commands = new AtomicLong();

    public long generation(Queue queue) {
        return counter(queue).get();
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
        };
    }
}
