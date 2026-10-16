package com.financeclaw.app.execution;

import com.financeclaw.app.execution.port.*;
import com.financeclaw.app.task.port.*;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;
import com.financeclaw.support.PayloadJson;
import java.util.*;
import org.springframework.stereotype.Service;

@Service
public class ExecutionPolicyService {
  private final TaskQueryGateway db;
  private final TaskMutationGateway mutations;
  private final ExecutionPolicyGateway governance;
  private final ExecutionRelease releases;
  private final ExecutionMemory memory;
  private final ExecutionLeaseService leases;

  public ExecutionPolicyService(
      TaskQueryGateway db,
      TaskMutationGateway mutations,
      ExecutionPolicyGateway governance,
      ExecutionRelease releases,
      ExecutionMemory memory,
      ExecutionLeaseService leases) {
    this.db = db;
    this.mutations = mutations;
    this.governance = governance;
    this.releases = releases;
    this.memory = memory;
    this.leases = leases;
  }

  public String memoryContext(Claim claim) {
    return memory.context(claim.turn());
  }

  public void checkDispatch(Claim claim) {
    leases.fenced(
        claim,
        () -> {
          if (!db.turn(claim.turn().id()).status().equals("running")) throw new Fenced();
          checkPolicy(claim);
          return null;
        });
  }

  /** Called with the command locked; privacy owner then grant serialize writes with revocation. */
  public Policy checkPolicy(Claim claim) {
    releases.require(claim.turn().releaseHash());
    memory.check(claim.turn());
    var p = governance.policy(claim.turn().id());
    var grant = governance.lockGrant(claim.turn().conversationId());
    if (p == null || grant == null || p.grantRevision() != grant.revision())
      throw new Blocked("authorization_changed");
    if (governance.uncertain(claim.turn().id())) throw new Blocked("reconciliation_required");
    return p;
  }

  public List<Turn> history(Claim claim) {
    return leases.fenced(
        claim,
        () -> {
          var p = checkPolicy(claim);
          return db
              .history(claim.turn().conversationId(), claim.turn().releaseHash(), p.grantRevision())
              .stream()
              .filter(memory::sameEpoch)
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
        List.copyOf(PayloadJson.scopes(p.scopes())),
        governance.startedAt(claim.command().attemptId()));
  }

  public void checkArtifactPrivacy(Turn turn) {
    if (!memory.sameEpoch(turn)) throw new Problem(409, "privacy_epoch_changed");
  }

  public void consume(Claim claim, boolean model) {
    leases.fenced(
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

  public void progress(Claim claim, String kind, String text, String tool) {
    if (!Set.of("text_delta", "tool_started", "tool_completed", "tool_failed").contains(kind))
      throw new IllegalArgumentException("Unknown public event");
    leases.fenced(
        claim,
        () -> {
          Turn t = mutations.lockTurn(claim.turn().id());
          if (!t.status().equals("running")) return null;
          checkPolicy(claim);
          mutations.advance(t.id());
          mutations.progress(t.id(), kind, text, tool);
          return null;
        });
  }

  public List<String> scopes(Claim claim) {
    return List.copyOf(PayloadJson.scopes(governance.policy(claim.turn().id()).scopes()));
  }
}
