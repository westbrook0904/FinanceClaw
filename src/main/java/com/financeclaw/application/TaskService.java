package com.financeclaw.application;

import com.financeclaw.agent.ResumeInputs;
import com.financeclaw.domain.ExecutionScope;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;
import com.financeclaw.infrastructure.governance.GovernanceMapper;
import com.financeclaw.infrastructure.state.RuntimeMapper;
import com.financeclaw.infrastructure.task.TaskMapper;
import java.util.List;
import java.util.Set;
import java.util.UUID;
import java.util.function.Supplier;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionTemplate;

/** Short transactions only. Command row first, then Turn row, in every execution/cancel path. */
@Service
public class TaskService {
  private final TaskMapper db;
  private final RuntimeMapper states;
  private final TransactionTemplate tx;

  private final GovernanceMapper governance;
  private final ReleaseCatalog releases;
  private MemoryService memory;

  @org.springframework.beans.factory.annotation.Autowired
  public void memory(MemoryService memory) {
    this.memory = memory;
  }

  public String memoryContext(Claim claim) {
    return memory == null ? "" : memory.context(claim.turn());
  }

  public TaskService(
      TaskMapper db,
      RuntimeMapper states,
      TransactionTemplate tx,
      GovernanceMapper governance,
      ReleaseCatalog releases) {
    this.db = db;
    this.states = states;
    this.tx = tx;
    this.governance = governance;
    this.releases = releases;
  }

  private static String id() {
    return UUID.randomUUID().toString();
  }

  public String createConversation(Owner owner) {
    String id = id();
    tx.executeWithoutResult(
        s -> {
          db.createConversation(id, owner);
          governance.ensureGrant(
              id, ResumeInputs.canonical(releases.require(releases.currentHash()).scopes()));
        });
    return id;
  }

  public void owned(Owner owner, String conversation) {
    if (db.ownedConversation(conversation, owner) == null) throw new Problem(404, "not_found");
  }

  public Turn get(Owner owner, String conversation, String turn) {
    owned(owner, conversation);
    Turn t = db.turn(turn);
    if (t == null || !t.conversationId().equals(conversation)) throw new Problem(404, "not_found");
    return t;
  }

  public Accepted submit(Owner owner, String conversation, String key, String message) {
    validateKey(key);
    if (message == null || message.isBlank() || message.length() > 32000)
      throw new Problem(422, "invalid_message");
    String hash = ExecutionScope.hash(List.of(message));
    return tx.execute(
        s -> {
          if (db.lockConversation(conversation, owner) == null) throw new Problem(404, "not_found");
          Turn old = db.replay(conversation, key);
          if (old != null) {
            if (!old.requestHash().equals(hash)) throw new Problem(409, "idempotency_conflict");
            // Return the original admission snapshot even if execution has already advanced.
            return new Accepted(conversation, old.id(), "queued", 1, true);
          }
          if (db.active(conversation)) throw new Problem(409, "conversation_busy");
          Turn t =
              new Turn(
                  id(),
                  conversation,
                  owner.tenant(),
                  owner.subject(),
                  key,
                  hash,
                  message,
                  releases.currentHash(),
                  "queued",
                  1,
                  null,
                  null);
          db.insertTurn(t);
          if (memory != null) memory.admit(t);
          var grant = governance.lockGrant(conversation);
          if (grant == null) throw new Problem(409, "grant_unavailable");
          governance.insertPolicy(
              t.id(),
              grant.revision(),
              ResumeInputs.canonical(
                  GovernanceService.scopes(grant.scopes()).stream()
                      .filter(releases.require(t.releaseHash()).scopes()::contains)
                      .toList()));
          db.insertCommand(id(), t.id());
          db.journal(conversation, t.id(), "user", message);
          db.event(t.id());
          return accepted(t, false);
        });
  }

  public static void validateKey(String key) {
    if (key == null || key.isBlank() || key.length() > 256)
      throw new Problem(422, "invalid_idempotency_key");
  }

  public List<Journal> messages(Owner owner, String conversation, long after, int limit) {
    owned(owner, conversation);
    if (after < 0 || limit < 1 || limit > 500) throw new Problem(422, "invalid_pagination");
    return db.messages(conversation, after, limit);
  }

  public List<Event> events(Owner owner, String conversation, String turn, long after) {
    Turn t = get(owner, conversation, turn);
    if (after < 0 || after > t.revision()) throw new Problem(422, "invalid_event_cursor");
    return db.events(turn, after);
  }

  public Accepted cancel(Owner owner, String conversation, String turn, String key) {
    validateKey(key);
    get(owner, conversation, turn);
    return tx.execute(
        s -> {
          Command c = db.lockTurnCommand(turn);
          Turn t = db.lockTurn(turn);
          Accepted replay = db.cancelReplay(turn, key);
          if (replay != null) return replay;
          if (governance.uncertain(turn)) throw new Problem(409, "reconciliation_required");
          if (!t.terminal() && !t.status().equals("cancelling")) {
            if (c.status().equals("running")) change(t, "cancelling", null, "cancel_requested");
            else {
              change(t, "cancelled", null, "cancel_requested");
              db.finishCommand(c.id());
            }
          }
          t = db.turn(turn);
          db.recordCancel(turn, key, t.status(), t.revision());
          return accepted(t, false);
        });
  }

  public Claim claim(String worker, long leaseMillis) {
    return tx.execute(
        s -> {
          Command c = db.nextCommand();
          if (c == null) return null;
          Turn t = db.lockTurn(c.turnId());
          if (!Set.of("queued", "resuming").contains(t.status()))
            throw new IllegalStateException("Command/Turn mismatch");
          db.claim(c.id(), id(), worker, leaseMillis);
          c = db.lockCommand(c.id());
          db.insertAttempt(c);
          change(t, "running", null, null);
          return new Claim(c, db.turn(t.id()));
        });
  }

  private Command current(Claim claim) {
    Command now = db.lockCommand(claim.command().id());
    if (now == null
        || !now.live()
        || !now.status().equals("running")
        || now.fence() != claim.command().fence()
        || !now.attemptId().equals(claim.command().attemptId())) throw new Fenced();
    return now;
  }

  public <T> T fenced(Claim claim, Supplier<T> action) {
    return tx.execute(
        s -> {
          current(claim);
          return action.get();
        });
  }

  public boolean heartbeat(Claim claim, long leaseMillis) {
    return fenced(
        claim,
        () -> {
          db.renew(claim.command().id(), leaseMillis);
          return db.turn(claim.turn().id()).status().equals("cancelling");
        });
  }

  public void checkDispatch(Claim claim) {
    fenced(
        claim,
        () -> {
          if (!db.turn(claim.turn().id()).status().equals("running")) throw new Fenced();
          checkPolicy(claim);
          return null;
        });
  }

  public ReleaseCatalog releases() {
    return releases;
  }

  public GovernanceMapper governance() {
    return governance;
  }

  /** Called under the command fence. Holding the grant row serializes dispatch with revocation. */
  public Policy checkPolicy(Claim claim) {
    releases.require(claim.turn().releaseHash());
    if (memory != null) memory.check(claim.turn());
    var p = governance.policy(claim.turn().id());
    var grant = governance.lockGrant(claim.turn().conversationId());
    if (p == null || grant == null || p.grantRevision() != grant.revision())
      throw new Blocked("authorization_changed");
    if (governance.uncertain(claim.turn().id())) throw new Blocked("reconciliation_required");
    return p;
  }

  public List<Turn> history(Claim claim) {
    return fenced(
        claim,
        () -> {
          var p = checkPolicy(claim);
          return db
              .history(claim.turn().conversationId(), claim.turn().releaseHash(), p.grantRevision())
              .stream()
              .filter(t -> memory == null || memory.sameEpoch(t))
              .toList();
        });
  }

  public ExecutionIdentity identity(Claim claim) {
    var p = governance.policy(claim.turn().id());
    var t = claim.turn();
    return new ExecutionIdentity(
        t.tenant(),
        t.subject(),
        t.conversationId(),
        t.id(),
        claim.command().attemptId(),
        t.releaseHash(),
        p.grantRevision(),
        List.copyOf(GovernanceService.scopes(p.scopes())),
        governance.startedAt(claim.command().attemptId()));
  }

  public void consume(Claim claim, boolean model) {
    fenced(
        claim,
        () -> {
          if (!db.turn(claim.turn().id()).status().equals("running")) throw new Fenced();
          checkPolicy(claim);
          var r = releases.require(claim.turn().releaseHash());
          int changed =
              model
                  ? governance.modelCall(claim.turn().id(), r.maxModelCalls())
                  : governance.toolCall(claim.turn().id(), r.maxToolCalls());
          if (changed != 1) throw new Blocked("execution_budget_exhausted");
          return null;
        });
  }

  public void block(Claim claim, String reason) {
    fenced(
        claim,
        () -> {
          var t = db.lockTurn(claim.turn().id());
          change(t, "blocked", null, reason);
          db.finishCommand(claim.command().id());
          return null;
        });
  }

  public void progress(Claim claim, String kind, String text, String tool) {
    if (!Set.of("text_delta", "tool_started", "tool_completed", "tool_failed").contains(kind))
      throw new IllegalArgumentException("Unknown public event");
    fenced(
        claim,
        () -> {
          Turn t = db.lockTurn(claim.turn().id());
          if (!t.status().equals("running")) return null;
          checkPolicy(claim);
          db.advance(t.id());
          db.progress(t.id(), kind, text, tool);
          return null;
        });
  }

  public void seal(Claim claim, Outcome outcome) {
    if (!Set.of("completed", "waiting", "failed").contains(outcome.status()))
      throw new IllegalArgumentException("Invalid outcome");
    fenced(
        claim,
        () -> {
          checkPolicy(claim);
          var scope = claim.scope();
          var row =
              states.findState(
                  new RuntimeMapper.StateKey(
                      scope.namespace(), scope.userId(), scope.sessionId(), "agent_state"));
          if (row == null || row.deleted() || row.version() != outcome.stateVersion())
            throw new Fenced();
          if (db.seal(claim.command().attemptId(), outcome) != 1) throw new Fenced();
          if (outcome.status().equals("waiting")) {
            var policy = governance.policy(claim.turn().id());
            for (var p : outcome.pending())
              governance.insertInteraction(
                  new Interaction(
                      id(),
                      claim.turn().id(),
                      claim.command().id(),
                      claim.command().attemptId(),
                      outcome.stateVersion(),
                      policy.grantRevision(),
                      claim.turn().releaseHash(),
                      p.kind(),
                      p.callId(),
                      p.tool(),
                      p.actionHash(),
                      p.schema(),
                      p.question(),
                      1,
                      "pending",
                      true,
                      null,
                      null));
          }
          return null;
        });
  }

  public void finish(Claim claim) {
    fenced(
        claim,
        () -> {
          finishReceipt(db.lockCommand(claim.command().id()), db.lockTurn(claim.turn().id()));
          return null;
        });
  }

  private void finishReceipt(Command c, Turn t) {
    Receipt r = db.receipt(c.attemptId());
    if (r == null || r.outcome() == null || r.fence() != c.fence())
      throw new IllegalStateException("Missing sealed execution receipt");
    var scope = new Claim(c, t).scope();
    var row =
        states.findState(
            new RuntimeMapper.StateKey(
                scope.namespace(), scope.userId(), scope.sessionId(), "agent_state"));
    if (!t.status().equals("cancelling")) {
      try {
        checkPolicy(new Claim(c, t));
      } catch (Blocked blocked) {
        change(t, "blocked", null, blocked.getMessage());
        db.finishCommand(c.id());
        return;
      }
    }
    if (row == null || row.deleted() || row.version() != r.stateVersion()) {
      change(t, "blocked", null, "checkpoint_mismatch");
    } else if (t.status().equals("cancelling")) change(t, "cancelled", null, "cancel_confirmed");
    else
      change(t, r.outcome(), r.result(), r.outcome().equals("failed") ? "execution_failed" : null);
    db.finishCommand(c.id());
  }

  /**
   * Called only after the SDK call and its resources have stopped. No completion snapshot is
   * required.
   */
  public boolean acknowledgeCancel(Claim claim) {
    return fenced(
        claim,
        () -> {
          Turn t = db.lockTurn(claim.turn().id());
          if (!t.status().equals("cancelling")) return false;
          change(t, "cancelled", null, "cancel_confirmed");
          db.finishCommand(claim.command().id());
          return true;
        });
  }

  public void fail(Claim claim) {
    fenced(
        claim,
        () -> {
          Turn t = db.lockTurn(claim.turn().id());
          change(
              t,
              t.status().equals("cancelling") ? "cancelled" : "failed",
              null,
              t.status().equals("cancelling") ? "cancel_confirmed" : "execution_failed");
          db.finishCommand(claim.command().id());
          return null;
        });
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
                    Turn t = db.lockTurn(c.turnId());
                    Receipt r = db.receipt(c.attemptId());
                    if (r != null && r.outcome() != null) finishReceipt(c, t);
                    else {
                      change(t, "blocked", null, "unconfirmed_execution");
                      db.finishCommand(id);
                    }
                    return true;
                  }));
      if (changed) count++;
    }
    return count;
  }

  public boolean projectNext() {
    return Boolean.TRUE.equals(
        tx.execute(
            s -> {
              Delivery d = db.nextDelivery();
              if (d == null) return false;
              db.project(d);
              db.delivered(d.id());
              return true;
            }));
  }

  public void change(Turn t, String status, String result, String reason) {
    if (t.terminal()) throw new Fenced();
    if (!Set.of("waiting", "running", "resuming").contains(status)) governance.invalidate(t.id());
    db.transition(t.id(), status, result, reason);
    db.event(t.id());
    if (Set.of("completed", "failed", "cancelled").contains(status)) {
      if (status.equals("completed")) db.journal(t.conversationId(), t.id(), "assistant", result);
      if (status.equals("completed") && memory != null) memory.completed(t.id());
      db.outbox(t.id());
    }
  }

  private Accepted accepted(Turn t, boolean replay) {
    return new Accepted(t.conversationId(), t.id(), t.status(), t.revision(), replay);
  }
}
