package com.financeclaw.domain;

import java.util.List;
import java.util.Set;

/** Business identities and immutable execution bindings; independent of Spring and AgentScope. */
public final class Task {
  private Task() {}

  public record Owner(String tenant, String subject) {
    public Owner {
      if (tenant == null || tenant.isBlank() || subject == null || subject.isBlank())
        throw new IllegalArgumentException("Missing owner");
    }
  }

  public record Turn(
      String id,
      String conversationId,
      String tenant,
      String subject,
      String idempotencyKey,
      String requestHash,
      String message,
      String releaseHash,
      String status,
      long revision,
      String result,
      String reason) {
    public boolean terminal() {
      return Set.of("completed", "failed", "cancelled").contains(status);
    }

    public Accepted accepted(boolean replay) {
      return new Accepted(conversationId, id, status, revision, replay);
    }

    public Accepted replayAdmission(String hash) {
      if (!requestHash.equals(hash)) throw new Problem(409, "idempotency_conflict");
      return new Accepted(conversationId, id, "queued", 1, true);
    }

    public void requireClaimable() {
      if (!Set.of("queued", "resuming").contains(status))
        throw new IllegalStateException("Command/Turn mismatch");
    }
  }

  public record Command(
      String id,
      String turnId,
      String status,
      String attemptId,
      long fence,
      boolean live,
      String resumeId) {}

  public record Claim(Command command, Turn turn) {
    public void requireCurrent(Command now) {
      if (now == null
          || !now.live()
          || !now.status().equals("running")
          || now.fence() != command.fence()
          || !java.util.Objects.equals(now.attemptId(), command.attemptId())) throw new Fenced();
    }

    public ExecutionScope scope() {
      return new ExecutionScope(
          turn.tenant(),
          turn.subject(),
          ExecutionScope.hash(
              List.of(turn.conversationId(), turn.id(), command.id(), command.attemptId())),
          "finance_agent",
          turn.releaseHash());
    }
  }

  public record Receipt(
      String id, String commandId, long fence, Long stateVersion, String outcome, String result) {}

  public record Outcome(
      String status, String result, long stateVersion, List<Governance.Pending> pending) {
    public Outcome(String status, String result, long version) {
      this(status, result, version, List.of());
    }
  }

  public record Accepted(
      String conversationId,
      String turnId,
      String status,
      long revision,
      boolean idempotentReplay) {}

  public record Event(
      String turnId,
      long revision,
      String status,
      String reason,
      String kind,
      String text,
      String tool) {
    public Event(String turnId, long revision, String status, String reason) {
      this(turnId, revision, status, reason, "turn", null, null);
    }
  }

  public record Journal(
      long sequence, String conversationId, String turnId, String role, String content) {}

  public static final class Problem extends RuntimeException {
    private final int status;

    public Problem(int status, String code) {
      super(code);
      this.status = status;
    }

    public int status() {
      return status;
    }
  }

  public static final class Fenced extends RuntimeException {
    public Fenced() {
      super("Execution lease is no longer current");
    }
  }
}
