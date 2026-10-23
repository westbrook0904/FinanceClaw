package com.financeclaw.infrastructure.task;

import com.financeclaw.app.execution.port.CapabilityStepGateway;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.StepResult;
import com.financeclaw.domain.Task.*;
import com.financeclaw.infrastructure.governance.GovernanceMapper;
import org.springframework.stereotype.Repository;

@Repository
public class MyBatisCapabilityStepGateway implements CapabilityStepGateway {
  private final GovernanceMapper db;

  public MyBatisCapabilityStepGateway(GovernanceMapper db) {
    this.db = db;
  }

  public StepResult step(String turn, String namespace, String step) {
    var saved = db.getCapabilityStep(turn, namespace, step);
    return saved == null ? null : new StepResult(saved.inputHash(), saved.result());
  }

  public void saveStep(String turn, String namespace, String step, String hash, String result) {
    db.insertCapabilityStep(turn, namespace, step, hash, result);
  }
}
