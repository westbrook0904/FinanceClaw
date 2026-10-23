package com.financeclaw.app.operations;

import com.financeclaw.app.operations.port.AdmissionGateway;
import com.financeclaw.domain.IdempotencyKey;
import com.financeclaw.domain.Operations.*;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.Task.Problem;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionTemplate;

/** 按用户控制新任务受理：修改使用版本号和幂等键串行化；暂停只阻止新任务，不改变既有任务的执行和恢复。 */
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

    /** 串行修改用户受理状态，并保留不可变回执。暂停与已经持有共享锁的受理之间，以事务提交先后确定边界。 */
    public Receipt change(Owner owner, String key, boolean paused, long revision) {
        var request = new AdmissionChange(new IdempotencyKey(key), paused, revision);
        return tx.execute(
                status -> {
                    var current = admission.lock(owner);
                    var previous = admission.findChange(owner, key);
                    // Replay before checking the current revision: an old receipt never reapplies
                    // its change.
                    if (previous != null) {
                        return previous.replay(request);
                    }
                    var next = current.advance(request);
                    if (!admission.update(owner, next, revision)) {
                        throw new Problem(409, "admission_revision_changed");
                    }
                    admission.record(owner, request, next);
                    return new Receipt(next.paused(), next.revision(), false);
                });
    }
}
