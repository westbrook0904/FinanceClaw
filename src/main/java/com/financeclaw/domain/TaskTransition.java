package com.financeclaw.domain;

import com.financeclaw.domain.Task.*;
import java.util.Set;

/** Consequences of a task transition, independent of transaction and persistence mechanisms. */
public record TaskTransition(Turn turn, String status, String result, String reason) {
  public TaskTransition {
    if (turn.terminal()) throw new Fenced();
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
