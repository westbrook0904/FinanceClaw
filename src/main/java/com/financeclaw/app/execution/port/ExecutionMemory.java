package com.financeclaw.app.execution.port;

import com.financeclaw.domain.Task.Turn;
import java.util.List;

public interface ExecutionMemory {
  void admit(Turn turn);

  void check(Turn turn);

  boolean sameEpoch(Turn turn);

  default List<Turn> filterHistory(Turn current, List<Turn> history) {
    return history.stream().filter(this::sameEpoch).toList();
  }

  void completed(String turn);

  String context(Turn turn);
}
