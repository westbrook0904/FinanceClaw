package com.financeclaw.infrastructure.operations;

import com.financeclaw.app.operations.port.AdmissionGateway;
import com.financeclaw.domain.Operations.*;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.infrastructure.task.TaskMapper;
import org.springframework.stereotype.Repository;

/**
 * 实现 AdmissionGateway，按 owner 读取、锁定和修改受理开关。 修改前确保控制行存在并取得排他锁，更新还核对期望 revision；状态与幂等回执由
 * AdmissionService 放在同一事务内提交。该开关不终止既有任务。
 */
@Repository
public class MyBatisAdmissionGateway implements AdmissionGateway {
    private final TaskMapper tasks;

    public MyBatisAdmissionGateway(TaskMapper tasks) {
        this.tasks = tasks;
    }

    @Override
    public Admission current(Owner owner) {
        return tasks.getAdmission(owner);
    }

    @Override
    public Admission lock(Owner owner) {
        tasks.insertAdmissionIfAbsent(owner);
        return tasks.getAdmissionForUpdate(owner);
    }

    @Override
    public Change findChange(Owner owner, String key) {
        return tasks.getAdmissionChangeByIdempotencyKey(owner, key);
    }

    @Override
    public boolean update(Owner owner, Admission next, long expectedRevision) {
        return tasks.updateAdmissionByRevision(owner, next.paused(), expectedRevision) == 1;
    }

    @Override
    public void record(Owner owner, AdmissionChange request, Admission next) {
        tasks.insertAdmissionChange(
                owner,
                request.key().value(),
                request.requestHash(),
                next.paused(),
                next.revision());
    }
}
