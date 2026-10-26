package com.financeclaw.domain.scheduling;

import java.time.Instant;

/** Durable admission receipt, never a second execution state machine. */
public record TriggerFiring(
        String ruleId,
        long ruleRevision,
        String eventKey,
        Instant scheduledAt,
        String status,
        String conversationId,
        String turnId,
        String releaseHash,
        Instant coalescedUntil) {}
