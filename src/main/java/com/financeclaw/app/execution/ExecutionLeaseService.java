package com.financeclaw.app.execution;

import com.financeclaw.app.execution.port.*;
import com.financeclaw.app.task.TaskTransitionService;
import com.financeclaw.app.task.port.*;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;
import java.util.*;
import java.util.function.Supplier;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionTemplate;

/** Command is always locked before Turn. The fence and each write share one Spring transaction. */
@Service
public class ExecutionLeaseService {
  private final CommandGateway db;
  private final TaskMutationGateway turns;
  private final TaskQueryGateway reads;
  private final TaskTransitionService transitions;
  private final TransactionTemplate tx;

  public ExecutionLeaseService(
      CommandGateway db,
      TaskMutationGateway turns,
      TaskQueryGateway reads,
      TaskTransitionService transitions,
      TransactionTemplate tx) {
    this.db = db;
    this.turns = turns;
    this.reads = reads;
    this.transitions = transitions;
    this.tx = tx;
  }

  private static String id() {
    return UUID.randomUUID().toString();
  }

  public Claim claim(String worker, long leaseMillis) {
    return tx.execute(
        s -> {
          Command c = db.nextCommand();
          if (c == null) return null;
          Turn t = turns.lockTurn(c.turnId());
          t.requireClaimable();
          db.claim(c.id(), id(), worker, leaseMillis);
          c = db.lockCommand(c.id());
          db.insertAttempt(c);
          transitions.change(t, "running", null, null);
          return new Claim(c, reads.turn(t.id()));
        });
  }

  private void current(Claim claim) {
    claim.requireCurrent(db.lockCommand(claim.command().id()));
  }

  public <T> T fenced(Claim claim, Supplier<T> action) {
    return tx.execute(
        s -> {
          current(claim);
          return action.get();
        });
  }

  public boolean heartbeat(Claim claim, long leaseMillis) {
    if (leaseMillis <= 0) throw new IllegalArgumentException("Invalid lease duration");
    return tx.execute(
        s -> {
          Boolean cancelling = db.renew(claim, leaseMillis);
          if (cancelling == null) throw new Fenced();
          return cancelling;
        });
  }
}
