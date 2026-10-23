package com.financeclaw.worker;

import java.util.concurrent.TimeUnit;
import java.util.function.LongSupplier;

/** Local monotonic backoff; a commit or capacity signal cannot be overwritten by an empty query. */
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
        || maximumMillis > TimeUnit.DAYS.toMillis(1))
      throw new IllegalArgumentException("Invalid idle polling limits");
    initialDelay = TimeUnit.MILLISECONDS.toNanos(initialMillis);
    maximumDelay = TimeUnit.MILLISECONDS.toNanos(maximumMillis);
    this.clock = java.util.Objects.requireNonNull(clock);
    nextDelay = initialDelay;
  }

  /** Capture this ticket before querying; pass that exact ticket back when the query is empty. */
  public synchronized Ticket acquire(long generation) {
    if (!observed || generation != observedGeneration) {
      observed = true;
      observedGeneration = generation;
      reset();
    }
    if (waiting && clock.getAsLong() - deadline < 0) return null;
    return new Ticket(observedGeneration, wakeVersion);
  }

  public synchronized void empty(Ticket ticket) {
    if (ticket.generation() != observedGeneration || ticket.wakeVersion() != wakeVersion) return;
    deadline = clock.getAsLong() + nextDelay;
    waiting = true;
    nextDelay = Math.min(maximumDelay, nextDelay * 2);
  }

  /**
   * Real work starts the next idle period at the initial delay, without invalidating its ticket.
   */
  public synchronized void worked() {
    reset();
  }

  /** Capacity becoming available permits a new scan on the next local scheduling tick. */
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
