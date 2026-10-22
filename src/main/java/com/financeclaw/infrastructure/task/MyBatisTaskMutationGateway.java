package com.financeclaw.infrastructure.task;

import com.financeclaw.app.task.TaskEventHub;
import com.financeclaw.app.task.port.TaskMutationGateway;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;
import org.springframework.stereotype.Repository;

@Repository
public class MyBatisTaskMutationGateway implements TaskMutationGateway {
  private final TaskMapper db;
  private final TaskEventHub events;

  public MyBatisTaskMutationGateway(TaskMapper db, TaskEventHub events) {
    this.events = events;
    this.db = db;
  }

  public Turn lockTurn(String id) {
    return db.lockTurn(id);
  }

  public Accepted cancelReplay(String turn, String key) {
    return db.cancelReplay(turn, key);
  }

  public void recordCancel(String turn, String key, String status, long revision) {
    db.recordCancel(turn, key, status, revision);
  }

  public void transition(String turn, String status, String result, String reason) {
    db.transition(turn, status, result, reason);
  }

  public void event(String turn) {
    events.committed(db.event(turn));
  }

  public void journal(String conversation, String turn, String role, String content) {
    db.journal(conversation, turn, role, content);
  }

  public void outbox(String turn) {
    db.outbox(turn);
  }
}
