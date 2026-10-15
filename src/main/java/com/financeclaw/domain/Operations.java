package com.financeclaw.domain;

import java.util.List;
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

  public interface BacklogItem {
    String id();
  }

  public record BacklogPage<T extends BacklogItem>(String scope, List<T> items, String nextAfter) {}

  public record TurnBacklog(
      String id,
      String conversationId,
      String status,
      long revision,
      String reason,
      String commandStatus,
      long pendingInteractions,
      long uncertainEffects,
      long pendingOutbox)
      implements BacklogItem {}

  public record NotificationBacklog(
      String id, String conversationId, String turnId, String status, long revision)
      implements BacklogItem {}

  public record BindingBacklog(
      String id, String conversationId, String turnId, String status, long revision)
      implements BacklogItem {}

  public record MemoryBacklog(
      String id,
      String kind,
      String status,
      long epoch,
      long revision,
      int attempts,
      String errorCode)
      implements BacklogItem {}
}
