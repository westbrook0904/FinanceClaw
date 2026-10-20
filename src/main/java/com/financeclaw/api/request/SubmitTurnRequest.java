package com.financeclaw.api.request;

import jakarta.validation.constraints.NotNull;

/** HTTP shape only; message semantics follow idempotency-key validation in the use case. */
public record SubmitTurnRequest(@NotNull String message) {}
