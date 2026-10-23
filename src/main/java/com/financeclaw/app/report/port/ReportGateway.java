package com.financeclaw.app.report.port;

import com.financeclaw.domain.Governance.Effect;

/** Report persistence and effect receipts, used inside the execution fence transaction. */
public interface ReportGateway {
  Effect effect(String turn, String call);

  /** Inserts the report and completed receipt together; callers hold the execution fence. */
  void publishReport(String id, String turn, String call, String title, String hash, String result);
}
