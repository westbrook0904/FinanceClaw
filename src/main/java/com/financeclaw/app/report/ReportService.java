package com.financeclaw.app.report;

import com.financeclaw.app.execution.ExecutionLeaseService;
import com.financeclaw.app.execution.ExecutionPolicyService;
import com.financeclaw.app.report.port.ReportGateway;
import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.Task.Claim;
import com.financeclaw.support.PayloadJson;
import java.util.Map;
import java.util.UUID;
import org.springframework.stereotype.Service;

/** Publishes one report and its durable effect receipt under the current execution lease. */
@Service
public class ReportService {
  private final ReportGateway db;
  private final ExecutionLeaseService leases;
  private final ExecutionPolicyService policy;

  public ReportService(
      ReportGateway db, ExecutionLeaseService leases, ExecutionPolicyService policy) {
    this.db = db;
    this.leases = leases;
    this.policy = policy;
  }

  public String report(Claim claim, String call, String actionHash, String title) {
    return leases.fenced(
        claim,
        () -> {
          policy.checkPolicy(claim);
          var old = db.effect(claim.turn().id(), call);
          if (old != null) {
            // A legacy report without a receipt cannot be replayed with invented action identity.
            if (old.actionHash() == null) throw new Blocked("reconciliation_required");
            if (!old.actionHash().equals(actionHash)) throw new Blocked("effect_conflict");
            if (!old.status().equals("completed") || old.result() == null)
              throw new Blocked("reconciliation_required");
            return old.result();
          }
          String id = UUID.randomUUID().toString();
          String result = PayloadJson.canonical(Map.of("report_id", id, "status", "created"));
          db.publishReport(id, claim.turn().id(), call, title, actionHash, result);
          return result;
        });
  }
}
