package com.financeclaw.app.execution;

import com.financeclaw.app.execution.port.*;
import com.financeclaw.app.task.TaskTransitionService;
import com.financeclaw.app.task.port.*;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;
import java.util.*;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionTemplate;

/** Reconciles expired commands from SQL receipts; an unconfirmed execution is never replayed. */
@Service
public class RecoverTaskService {
  private final CommandGateway db;
  private final TaskMutationGateway turns;
  private final ExecutionReceiptGateway receipts;
  private final ExecutionCompletionService completion;
  private final TaskTransitionService transitions;
  private final TransactionTemplate tx;

  public RecoverTaskService(
      CommandGateway db,
      TaskMutationGateway turns,
      ExecutionReceiptGateway receipts,
      ExecutionCompletionService completion,
      TaskTransitionService transitions,
      TransactionTemplate tx) {
    this.db = db;
    this.turns = turns;
    this.receipts = receipts;
    this.completion = completion;
    this.transitions = transitions;
    this.tx = tx;
  }

  public int recoverExpired() {
    int count = 0;
    for (String id : db.expired()) {
      boolean changed =
          Boolean.TRUE.equals(
              tx.execute(
                  s -> {
                    Command c = db.lockCommand(id);
                    if (c == null || c.live() || !c.status().equals("running")) return false;
                    Turn t = turns.lockTurn(c.turnId());
                    Receipt r = receipts.receipt(c.attemptId());
                    if (r != null && r.outcome() != null) completion.finishReceipt(c, t);
                    else {
                      transitions.change(t, "blocked", null, "unconfirmed_execution");
                      db.finishCommand(id);
                    }
                    return true;
                  }));
      if (changed) count++;
    }
    return count;
  }
}
