package com.financeclaw.infrastructure.execution;

import com.financeclaw.app.execution.port.ExecutionMemory;
import com.financeclaw.application.MemoryService;
import com.financeclaw.domain.Task.Turn;
import org.springframework.stereotype.Component;

@Component
public class GovernedExecutionMemory implements ExecutionMemory {
  private final MemoryService memory;

  public GovernedExecutionMemory(MemoryService memory) {
    this.memory = memory;
  }

  public void admit(Turn turn) {
    memory.admit(turn);
  }

  public void check(Turn turn) {
    memory.check(turn);
  }

  public boolean sameEpoch(Turn turn) {
    return memory.sameEpoch(turn);
  }

  public void completed(String turn) {
    memory.completed(turn);
  }

  public String context(Turn turn) {
    return memory.context(turn);
  }
}
