package com.financeclaw.api.request;

import jakarta.validation.constraints.NotNull;

/** Boxed fields distinguish a missing value from false or revision zero. */
public record ChangeAdmissionRequest(@NotNull Boolean paused, @NotNull Long revision) {}
