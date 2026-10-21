package com.financeclaw.domain.memory;

import com.financeclaw.domain.ExecutionScope;
import com.financeclaw.domain.IdempotencyKey;
import com.financeclaw.domain.Task.Owner;
import java.util.List;
import java.util.Objects;
import java.util.UUID;

/** Content-free, stable identity of an authorized memory erasure. */
public record ForgetFact(
    String eventId, String lineage, Owner owner, long fromEpoch, String key, String requestHash) {
  public ForgetFact {
    if (!UUID.fromString(eventId).toString().equals(eventId)
        || !UUID.fromString(lineage).toString().equals(lineage))
      throw new IllegalArgumentException("invalid_forget_fact");
    Objects.requireNonNull(owner);
    new IdempotencyKey(key);
    if (fromEpoch < 1 || fromEpoch == Long.MAX_VALUE || !hash(fromEpoch).equals(requestHash))
      throw new IllegalArgumentException("invalid_forget_fact");
  }

  public long toEpoch() {
    return fromEpoch + 1;
  }

  public static String hash(long epoch) {
    return ExecutionScope.hash(List.of("forget", Long.toString(epoch)));
  }
}
