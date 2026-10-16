package com.financeclaw.app.task;

import com.financeclaw.app.execution.port.*;
import com.financeclaw.app.task.port.*;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.IdempotencyKey;
import com.financeclaw.domain.Task.*;
import java.util.*;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionTemplate;

@Service
public class CancelTaskService {
  private final TaskQueryService queries;
  private final TaskQueryGateway reads;
  private final TaskMutationGateway db;
  private final CommandGateway commands;
  private final ExecutionPolicyGateway governance;
  private final TaskTransitionService transitions;
  private final TransactionTemplate tx;

  public CancelTaskService(
      TaskQueryService queries,
      TaskQueryGateway reads,
      TaskMutationGateway db,
      CommandGateway commands,
      ExecutionPolicyGateway governance,
      TaskTransitionService transitions,
      TransactionTemplate tx) {
    this.queries = queries;
    this.reads = reads;
    this.db = db;
    this.commands = commands;
    this.governance = governance;
    this.transitions = transitions;
    this.tx = tx;
  }

  public Accepted cancel(Owner owner, String conversation, String turn, String key) {
    new IdempotencyKey(key);
    queries.get(owner, conversation, turn);
    return tx.execute(
        s -> {
          Command c = commands.lockTurnCommand(turn);
          Turn t = db.lockTurn(turn);
          Accepted replay = db.cancelReplay(turn, key);
          if (replay != null) return replay;
          if (governance.uncertain(turn)) throw new Problem(409, "reconciliation_required");
          if (!t.terminal() && !t.status().equals("cancelling")) {
            if (c.status().equals("running"))
              transitions.change(t, "cancelling", null, "cancel_requested");
            else {
              transitions.change(t, "cancelled", null, "cancel_requested");
              commands.finishCommand(c.id());
            }
          }
          t = reads.turn(turn);
          db.recordCancel(turn, key, t.status(), t.revision());
          return t.accepted(false);
        });
  }
}
