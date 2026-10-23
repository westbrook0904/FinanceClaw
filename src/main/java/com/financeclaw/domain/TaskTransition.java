package com.financeclaw.domain;

import com.financeclaw.domain.Task.*;
import java.util.Set;

/** 任务状态转换规则：集中判断终态及交互失效条件，使受理、执行和恢复使用一致的业务语义。 */
public record TaskTransition(Turn turn, String status, String result, String reason) {
    public TaskTransition {
        if (turn.terminal()) {
            throw new Fenced();
        }
    }

    public boolean invalidatesInteractions() {
        return !Set.of("waiting", "running", "resuming").contains(status);
    }

    public boolean completed() {
        return status.equals("completed");
    }

    public boolean terminal() {
        return Set.of("completed", "failed", "cancelled").contains(status);
    }
}
