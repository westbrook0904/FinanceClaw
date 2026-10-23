package com.financeclaw.infrastructure.task;

import com.financeclaw.app.execution.port.ExecutionReceiptGateway;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;
import com.financeclaw.infrastructure.governance.GovernanceMapper;
import com.financeclaw.infrastructure.state.RuntimeMapper;
import org.springframework.stereotype.Repository;

@Repository
public class MyBatisExecutionReceiptGateway implements ExecutionReceiptGateway {
  private final TaskMapper db;
  private final RuntimeMapper states;
  private final GovernanceMapper governance;

  public MyBatisExecutionReceiptGateway(
      TaskMapper db, RuntimeMapper states, GovernanceMapper governance) {
    this.db = db;
    this.states = states;
    this.governance = governance;
  }

  public boolean matchesCheckpoint(Claim claim, long version) {
    var scope = claim.scope();
    var row =
        states.getState(
            new RuntimeMapper.StateKey(
                scope.namespace(), scope.userId(), scope.sessionId(), "agent_state"));
    return row != null && !row.deleted() && row.version() == version;
  }

  public int seal(String attempt, Outcome outcome) {
    return db.updateAttemptOutcomeIfUnsealed(attempt, outcome);
  }

  public Receipt receipt(String attempt) {
    return db.getAttemptReceipt(attempt);
  }

  public void insertInteraction(Interaction interaction) {
    governance.insertInteraction(interaction);
  }
}
