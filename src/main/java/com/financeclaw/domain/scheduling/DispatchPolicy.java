package com.financeclaw.domain.scheduling;

import com.financeclaw.domain.Task.Problem;

public record DispatchPolicy(long revision, int maximumRunning, int maximumPerAgent) {
    public void validate() {
        if (revision < 0
                || revision == Long.MAX_VALUE
                || maximumRunning < 1
                || maximumRunning > 1024
                || maximumPerAgent < 1
                || maximumPerAgent > maximumRunning)
            throw new Problem(422, "invalid_dispatch_policy");
    }
}
