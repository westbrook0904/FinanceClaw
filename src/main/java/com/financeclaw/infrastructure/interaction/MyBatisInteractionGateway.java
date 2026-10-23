package com.financeclaw.infrastructure.interaction;

import com.financeclaw.app.execution.ExecutionPolicyService;
import com.financeclaw.app.interaction.port.InteractionGateway;
import com.financeclaw.app.task.TaskQueryService;
import com.financeclaw.app.task.TaskTransitionService;
import com.financeclaw.app.task.WorkSignals;
import com.financeclaw.domain.Governance.Interaction;
import com.financeclaw.domain.InteractionBinding;
import com.financeclaw.domain.Task.*;
import com.financeclaw.infrastructure.governance.GovernanceMapper;
import com.financeclaw.infrastructure.task.TaskMapper;
import java.util.List;
import org.springframework.stereotype.Repository;

@Repository
public class MyBatisInteractionGateway implements InteractionGateway {
  private final TaskQueryService queries;
  private final ExecutionPolicyService policies;
  private final TaskTransitionService transitions;
  private final TaskMapper turns;
  private final GovernanceMapper db;
  private final WorkSignals signals;

  public MyBatisInteractionGateway(
      TaskQueryService queries,
      ExecutionPolicyService policies,
      TaskTransitionService transitions,
      TaskMapper turns,
      GovernanceMapper db,
      WorkSignals signals) {
    this.signals = signals;
    this.queries = queries;
    this.policies = policies;
    this.transitions = transitions;
    this.turns = turns;
    this.db = db;
  }

  public Turn ownedTurn(Owner owner, String conversation, String turn) {
    return queries.get(owner, conversation, turn);
  }

  public Interaction find(String id) {
    return db.getInteractionById(id);
  }

  public Turn turn(String id) {
    return turns.getTurnById(id);
  }

  public InteractionBinding lock(String turn, String id) {
    // Preserve command -> turn -> interaction -> owner/grant lock ordering.
    var command = turns.getCommandByTurnIdForUpdate(turn);
    var task = turns.getTurnForUpdate(turn);
    var interaction = db.getInteractionForUpdate(id);
    if (interaction == null || !interaction.turnId().equals(turn))
      throw new Problem(404, "not_found");
    return new InteractionBinding(new Claim(command, task), interaction);
  }

  public void checkPolicy(Claim claim) {
    policies.checkPolicy(claim);
  }

  public long grantRevision(String conversation) {
    return db.getGrant(conversation).revision();
  }

  public void answer(String id, String hash, String json) {
    db.updatePendingInteractionAnswer(id, hash, json);
  }

  public List<Interaction> interactions(String turn) {
    return db.listInteractionsByTurnId(turn);
  }

  public void resume(InteractionBinding binding) {
    if (db.updateDoneCommandForResume(binding.claim().command().id(), binding.interaction().id())
        != 1) throw new Problem(409, "stale_interaction");
    transitions.change(binding.claim().turn(), "resuming", null, null);
    signals.afterCommit(WorkSignals.Queue.COMMAND);
  }
}
