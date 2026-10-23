package com.financeclaw.app.operations.port;

import com.financeclaw.domain.Operations.*;
import com.financeclaw.domain.Task.Owner;

/** 用户受理控制的存储边界：共享检查锁与排他修改锁保证暂停和新任务受理之间存在明确提交顺序。 */
public interface AdmissionGateway {
    Admission current(Owner owner);

    /**
     * Create the default row if absent, then hold its exclusive lock until the transaction ends.
     */
    Admission lock(Owner owner);

    Change findChange(Owner owner, String key);

    boolean update(Owner owner, Admission next, long expectedRevision);

    void record(Owner owner, AdmissionChange request, Admission next);
}
