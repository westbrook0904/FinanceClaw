package com.financeclaw.domain;

import com.financeclaw.domain.Task.Problem;
import java.util.Collections;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.TreeMap;

/**
 * Owner-scoped admission receipts and an observed drain snapshot, never a global cutover permit.
 */
public final class Operations {
  private Operations() {}

  public record Admission(boolean paused, long revision) {
    public Admission advance(AdmissionChange change) {
      if (revision != change.expectedRevision())
        throw new Problem(409, "admission_revision_changed");
      return new Admission(change.paused(), revision + 1);
    }
  }

  public record AdmissionChange(IdempotencyKey key, boolean paused, long expectedRevision) {
    public AdmissionChange {
      Objects.requireNonNull(key);
      if (expectedRevision < 0 || expectedRevision == Long.MAX_VALUE)
        throw new Problem(422, "invalid_revision");
    }

    public String requestHash() {
      return ExecutionScope.hash(
          List.of(Boolean.toString(paused), Long.toString(expectedRevision)));
    }
  }

  public record Change(String requestHash, boolean paused, long revision) {
    public Receipt replay(AdmissionChange request) {
      if (!requestHash.equals(request.requestHash()))
        throw new Problem(409, "idempotency_conflict");
      return new Receipt(paused, revision, true);
    }
  }

  public record Receipt(boolean paused, long revision, boolean idempotentReplay) {}

  public record Drain(
      String scope, Admission admission, Map<String, Long> counts, boolean drained) {
    public static Drain observed(Admission admission, Map<String, Long> counts) {
      var snapshot = Collections.unmodifiableMap(new TreeMap<>(counts));
      return new Drain(
          "current_owner",
          admission,
          snapshot,
          admission.paused() && snapshot.values().stream().allMatch(n -> n == 0));
    }
  }

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
