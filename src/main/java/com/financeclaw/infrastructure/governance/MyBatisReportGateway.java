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
    return db.getToolCallEffect(turn, call);
  }

  public void publishReport(
      String id, String turn, String call, String title, String hash, String result) {
    if (db.insertReportPublication(id, turn, call, title, hash, result) != 1)
      throw new IllegalStateException("Report publication did not insert exactly one row");
  }
}
