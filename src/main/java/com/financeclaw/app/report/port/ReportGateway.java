package com.financeclaw.app.report.port;

import com.financeclaw.domain.Governance.Effect;

/** Report persistence and effect receipts, used inside the execution fence transaction. */
public interface ReportGateway {
  Effect effect(String turn, String call);

  void beginEffect(String turn, String call, String hash);

  void report(String id, String turn, String call, String title);

  void completeEffect(String turn, String call, String result);
}
