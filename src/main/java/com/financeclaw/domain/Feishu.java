package com.financeclaw.domain;

/** 飞书身份与通知的领域值：绑定版本隔离会话切换，出站状态保留发送结果的不确定性。 */
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
