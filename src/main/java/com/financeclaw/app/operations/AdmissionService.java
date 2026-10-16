package com.financeclaw.app.operations;

import com.financeclaw.app.operations.port.AdmissionGateway;
import com.financeclaw.domain.IdempotencyKey;
import com.financeclaw.domain.Operations.*;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.Task.Problem;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionTemplate;

/** Changes admission for new Turns; existing AgentScope work keeps its normal recovery path. */
@Service
public class AdmissionService {
  private final AdmissionGateway admission;
  private final TransactionTemplate tx;

  public AdmissionService(AdmissionGateway admission, TransactionTemplate tx) {
    this.admission = admission;
    this.tx = tx;
  }

  public Admission admission(Owner owner) {
    var current = admission.current(owner);
    return current == null ? new Admission(false, 0) : current;
  }

  public Receipt change(Owner owner, String key, boolean paused, long revision) {
    var request = new AdmissionChange(new IdempotencyKey(key), paused, revision);
    return tx.execute(
        status -> {
          var current = admission.lock(owner);
          var previous = admission.findChange(owner, key);
          // Replay before checking the current revision: an old receipt never reapplies its change.
          if (previous != null) return previous.replay(request);
          var next = current.advance(request);
          if (!admission.update(owner, next, revision))
            throw new Problem(409, "admission_revision_changed");
          admission.record(owner, request, next);
          return new Receipt(next.paused(), next.revision(), false);
        });
  }
}
