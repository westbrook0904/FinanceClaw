package com.financeclaw.infrastructure.governance;

import com.financeclaw.app.report.port.ReportGateway;
import com.financeclaw.domain.Governance.Effect;
import org.springframework.stereotype.Repository;

@Repository
public class MyBatisReportGateway implements ReportGateway {
  private final GovernanceMapper db;

  public MyBatisReportGateway(GovernanceMapper db) {
    this.db = db;
  }

  public Effect effect(String turn, String call) {
    return db.effect(turn, call);
  }

  public void beginEffect(String turn, String call, String hash) {
    db.beginEffect(turn, call, hash);
  }

  public void report(String id, String turn, String call, String title) {
    db.report(id, turn, call, title);
  }

  public void completeEffect(String turn, String call, String result) {
    db.completeEffect(turn, call, result);
  }
}
