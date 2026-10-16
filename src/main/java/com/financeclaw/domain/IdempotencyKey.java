package com.financeclaw.domain;

import com.financeclaw.domain.Task.Problem;

/** An opaque client key; validation must not normalize an existing idempotency identity. */
public record IdempotencyKey(String value) {
  public IdempotencyKey {
    if (value == null || value.isBlank() || value.length() > 256)
      throw new Problem(422, "invalid_idempotency_key");
  }
}
