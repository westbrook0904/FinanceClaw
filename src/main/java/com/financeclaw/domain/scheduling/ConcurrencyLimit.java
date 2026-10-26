package com.financeclaw.domain.scheduling;

import com.financeclaw.domain.Task.Problem;

public record ConcurrencyLimit(long revision, int maximumRunning) {
    public void validate() {
        if (revision < 0
                || revision == Long.MAX_VALUE
                || maximumRunning < 1
                || maximumRunning > 4096) throw new Problem(422, "invalid_concurrency_limit");
    }
}
