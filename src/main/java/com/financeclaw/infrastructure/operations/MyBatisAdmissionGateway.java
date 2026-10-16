package com.financeclaw.infrastructure.operations;

import com.financeclaw.app.operations.port.AdmissionGateway;
import com.financeclaw.domain.Operations.*;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.infrastructure.task.TaskMapper;
import org.springframework.stereotype.Repository;

@Repository
public class MyBatisAdmissionGateway implements AdmissionGateway {
  private final TaskMapper tasks;

  public MyBatisAdmissionGateway(TaskMapper tasks) {
    this.tasks = tasks;
  }

  @Override
  public Admission current(Owner owner) {
    return tasks.admission(owner);
  }

  @Override
  public Admission lock(Owner owner) {
    tasks.ensureAdmission(owner);
    return tasks.lockAdmission(owner);
  }

  @Override
  public Change findChange(Owner owner, String key) {
    return tasks.admissionReplay(owner, key);
  }

  @Override
  public boolean update(Owner owner, Admission next, long expectedRevision) {
    return tasks.changeAdmission(owner, next.paused(), expectedRevision) == 1;
  }

  @Override
  public void record(Owner owner, AdmissionChange request, Admission next) {
    tasks.recordAdmission(
        owner, request.key().value(), request.requestHash(), next.paused(), next.revision());
  }
}
