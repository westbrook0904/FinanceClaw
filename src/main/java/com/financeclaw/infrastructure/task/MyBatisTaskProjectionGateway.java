package com.financeclaw.infrastructure.task;

import com.financeclaw.app.task.port.TaskProjectionGateway;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;
import org.springframework.stereotype.Repository;

@Repository
public class MyBatisTaskProjectionGateway implements TaskProjectionGateway {
  private final TaskMapper db;

  public MyBatisTaskProjectionGateway(TaskMapper db) {
    this.db = db;
  }

  public Delivery nextDelivery() {
    return db.nextDelivery();
  }

  public void project(Delivery delivery) {
    db.project(delivery);
  }

  public void delivered(long id) {
    db.delivered(id);
  }
}
