package com.financeclaw.infrastructure.task;

import com.financeclaw.app.execution.port.ExecutionPolicyGateway;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;
import com.financeclaw.infrastructure.governance.GovernanceMapper;
import org.springframework.stereotype.Repository;

@Repository
public class MyBatisExecutionPolicyGateway implements ExecutionPolicyGateway {
  private final GovernanceMapper db;

  public MyBatisExecutionPolicyGateway(GovernanceMapper db) {
    this.db = db;
  }

  public void ensureGrant(String conversation, String scopes) {
    db.ensureGrant(conversation, scopes);
  }

  public Grant lockGrant(String conversation) {
    return db.lockGrant(conversation);
  }

  public void insertPolicy(String turn, long revision, String scopes) {
    db.insertPolicy(turn, revision, scopes);
  }

  public Policy policy(String turn) {
    return db.policy(turn);
  }

  public boolean uncertain(String turn) {
    return db.uncertain(turn);
  }

  public int modelCall(String turn, int max) {
    return db.modelCall(turn, max);
  }

  public int toolCall(String turn, int max) {
    return db.toolCall(turn, max);
  }

  public void invalidate(String turn) {
    db.invalidate(turn);
  }

  public String startedAt(String attempt) {
    return db.startedAt(attempt);
  }
}
