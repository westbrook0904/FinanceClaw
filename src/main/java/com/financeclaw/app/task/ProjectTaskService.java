package com.financeclaw.app.task;

import com.financeclaw.app.execution.port.*;
import com.financeclaw.app.task.port.*;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;
import java.util.*;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionTemplate;

@Service
public class ProjectTaskService {
  private final TaskProjectionGateway db;
  private final TransactionTemplate tx;

  public ProjectTaskService(TaskProjectionGateway db, TransactionTemplate tx) {
    this.db = db;
    this.tx = tx;
  }

  public boolean projectNext() {
    return Boolean.TRUE.equals(
        tx.execute(
            s -> {
              Delivery d = db.nextDelivery();
              if (d == null) return false;
              db.project(d);
              db.delivered(d.id());
              return true;
            }));
  }
}
