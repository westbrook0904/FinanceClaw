package com.financeclaw.domain;

import java.util.Map;

/**
 * Owner-scoped admission receipts and an observed drain snapshot, never a global cutover permit.
 */
public final class Operations {
  private Operations() {}

  public record Admission(boolean paused, long revision) {}

  public record Change(String requestHash, boolean paused, long revision) {}

  public record Receipt(boolean paused, long revision, boolean idempotentReplay) {}

  public record Drain(
      String scope, Admission admission, Map<String, Long> counts, boolean drained) {}
}
