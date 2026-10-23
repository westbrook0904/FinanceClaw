package com.financeclaw.domain.memory;

import com.financeclaw.domain.ExecutionScope;
import com.financeclaw.domain.IdempotencyKey;
import com.financeclaw.domain.Task.Owner;
import java.util.List;
import java.util.Objects;
import java.util.UUID;

/** 独立遗忘事实：稳定事件身份、原始请求键和来源 epoch 一经追加不可更改，重试必须复用同一事实。 */
public record ForgetFact(
        String eventId,
        String lineage,
        Owner owner,
        long fromEpoch,
        String key,
        String requestHash) {
    public ForgetFact {
        if (!UUID.fromString(eventId).toString().equals(eventId)
                || !UUID.fromString(lineage).toString().equals(lineage)) {
            throw new IllegalArgumentException("invalid_forget_fact");
        }
        Objects.requireNonNull(owner);
        new IdempotencyKey(key);
        if (fromEpoch < 1 || fromEpoch == Long.MAX_VALUE || !hash(fromEpoch).equals(requestHash)) {
            throw new IllegalArgumentException("invalid_forget_fact");
        }
    }

    public long toEpoch() {
        return fromEpoch + 1;
    }

    public static String hash(long epoch) {
        return ExecutionScope.hash(List.of("forget", Long.toString(epoch)));
    }
}
