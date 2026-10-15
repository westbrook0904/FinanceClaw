package com.financeclaw.domain;

import java.util.List;

public final class Memory {
  private Memory() {}

  public record Privacy(String tenant, String subject, long epoch, boolean purgePending) {}

  public record Source(
      String id, String tenant, String subject, long epoch, String content, String status) {}

  public record Fact(
      String id,
      String sourceId,
      String tenant,
      String subject,
      long epoch,
      String key,
      String content,
      String evidence,
      long revision,
      String status) {}

  public record Job(
      long id,
      String kind,
      String keyId,
      String tenant,
      String subject,
      long epoch,
      long revision,
      String modelRevision,
      String status,
      int attempts,
      String leaseToken) {}

  public record Proposal(String key, String value, String quote) {}

  public record Extraction(List<Proposal> proposals) {}

  public record Operation(String requestHash, String result) {}
}
