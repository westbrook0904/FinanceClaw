package com.financeclaw.app.task;

import com.financeclaw.app.execution.port.*;
import com.financeclaw.app.task.port.*;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;
import java.util.*;
import org.springframework.stereotype.Service;

@Service
public class TaskTransitionService {
  private final TaskMutationGateway db;
  private final ExecutionPolicyGateway governance;
  private final ExecutionMemory memory;

  public TaskTransitionService(
      TaskMutationGateway db, ExecutionPolicyGateway governance, ExecutionMemory memory) {
    this.db = db;
    this.governance = governance;
    this.memory = memory;
  }

  /** Joins the caller's short transaction; transition, journal and memory commit together. */
  public void change(Turn t, String status, String result, String reason) {
    if (!org.springframework.transaction.support.TransactionSynchronizationManager
        .isActualTransactionActive())
      throw new IllegalStateException("Task transition requires a transaction");
    var transition = new com.financeclaw.domain.TaskTransition(t, status, result, reason);
    if (transition.invalidatesInteractions()) governance.invalidate(t.id());
    db.transition(t.id(), status, result, reason);
    db.event(t.id());
    if (transition.terminal()) {
      if (transition.completed()) db.journal(t.conversationId(), t.id(), "assistant", result);
      if (transition.completed()) memory.completed(t.id());
    }
  }
}
