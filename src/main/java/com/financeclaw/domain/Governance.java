package com.financeclaw.domain;

import java.util.List;

/** Persisted policy and interaction facts; all execution identities are assigned by the server. */
public final class Governance {
  private Governance() {}

  public record Grant(String conversationId, long revision, String scopes) {}

  public record Policy(
      String turnId, long grantRevision, String scopes, int modelCalls, int toolCalls) {}

  public record Pending(
      String kind, String callId, String tool, String actionHash, String schema, String question) {}

  public record Interaction(
      String id,
      String turnId,
      String commandId,
      String attemptId,
      long stateVersion,
      long grantRevision,
      String releaseHash,
      String kind,
      String callId,
      String tool,
      String actionHash,
      String schema,
      String question,
      long revision,
      String status,
      boolean live,
      String responseHash,
      String response) {}

  public record Answer(
      String kind,
      long revision,
      String decision,
      String actionHash,
      Object answer,
      String reason) {
    public Answer(String kind, long revision, String decision, String actionHash, Object answer) {
      this(kind, revision, decision, actionHash, answer, null);
    }
  }

  public record AnswerReceipt(
      String interactionId,
      String turnId,
      String status,
      long revision,
      boolean idempotentReplay) {}

  public record Artifact(
      String id,
      String turnId,
      String conversationId,
      String tenant,
      String subject,
      String releaseHash,
      String hash,
      String payload,
      int bytes) {}

  public record Page(
      String artifactId, String hash, int total, int start, int count, Object data) {}

  public record Effect(String actionHash, String status, String result) {}

  public record ExecutionIdentity(
      String tenant,
      String subject,
      String conversationId,
      String turnId,
      String attemptId,
      String releaseHash,
      long grantRevision,
      List<String> scopes,
      String startedAt) {}

  public static final class Blocked extends RuntimeException {
    public Blocked(String code) {
      super(code);
    }
  }
}
