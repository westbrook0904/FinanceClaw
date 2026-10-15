package com.financeclaw.domain;

public final class Feishu {
  private Feishu() {}

  public record Binding(
      String id,
      String tenantKey,
      String openId,
      String chatId,
      String tenant,
      String subject,
      String conversationId,
      long revision,
      boolean muted,
      String activeTurn) {}

  public record Link(String tenant, String subject, String conversationId, boolean live) {}

  public record Inbox(String bindingId, String requestHash, String receipt) {}

  public record Notice(
      String id,
      String turnId,
      String bindingId,
      long bindingRevision,
      long revision,
      String payload,
      String status,
      String reason) {}
}
