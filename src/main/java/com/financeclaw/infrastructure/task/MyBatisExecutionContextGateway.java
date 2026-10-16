package com.financeclaw.infrastructure.task;

import com.financeclaw.app.execution.port.ExecutionContextGateway;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;
import com.financeclaw.infrastructure.governance.GovernanceMapper;
import java.util.List;
import org.springframework.stereotype.Repository;

@Repository
public class MyBatisExecutionContextGateway implements ExecutionContextGateway {
  private final GovernanceMapper db;

  public MyBatisExecutionContextGateway(GovernanceMapper db) {
    this.db = db;
  }

  public Interaction interaction(String id) {
    return db.interaction(id);
  }

  public List<Interaction> interactions(String turn) {
    return db.interactions(turn);
  }

  public java.time.Instant requestClock(String turn) {
    return db.requestClock(turn);
  }
}
