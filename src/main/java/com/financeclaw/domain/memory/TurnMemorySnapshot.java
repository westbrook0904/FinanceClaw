package com.financeclaw.domain.memory;

import java.nio.charset.StandardCharsets;
import java.util.Objects;

/** 一个 Turn 受理时冻结的长期记忆正文；普通偏好编辑不改写它，遗忘按用户 epoch 擦除。 */
public record TurnMemorySnapshot(
        String turnId,
        String tenant,
        String subject,
        long epoch,
        long profileRevision,
        String context) {
    public static final int MAX_CONTEXT_BYTES = 262_144;

    public TurnMemorySnapshot {
        Objects.requireNonNull(turnId);
        Objects.requireNonNull(tenant);
        Objects.requireNonNull(subject);
        Objects.requireNonNull(context);
        if (epoch < 1
                || profileRevision < 0
                || context.getBytes(StandardCharsets.UTF_8).length > MAX_CONTEXT_BYTES) {
            throw new IllegalArgumentException("Invalid Turn memory snapshot");
        }
    }
}
