package com.financeclaw.app.execution.port;

import com.financeclaw.domain.Task.Turn;

public interface ExecutionMemory {
  void admit(Turn turn);

  void check(Turn turn);

  boolean sameEpoch(Turn turn);

  void completed(String turn);

  String context(Turn turn);
}
