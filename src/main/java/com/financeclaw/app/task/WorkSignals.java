package com.financeclaw.app.task;

import java.util.concurrent.atomic.AtomicLong;
import org.springframework.stereotype.Component;
import org.springframework.transaction.support.TransactionSynchronization;
import org.springframework.transaction.support.TransactionSynchronizationManager;

/**
 * Constant-space local wake-up hints. Durable queues and bounded fallback scans remain
 * authoritative.
 */
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
