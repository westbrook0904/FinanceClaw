package com.financeclaw.infrastructure.task;

import com.financeclaw.app.task.port.TaskAdmissionGateway;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;
import org.springframework.stereotype.Repository;

@Repository
public class MyBatisTaskAdmissionGateway implements TaskAdmissionGateway {
  private final TaskMapper db;

  public MyBatisTaskAdmissionGateway(TaskMapper db) {
    this.db = db;
  }

  public void createConversation(String id, Owner owner) {
    db.createConversation(id, owner);
  }

  public String lockConversation(String id, Owner owner) {
    return db.lockConversation(id, owner);
  }

  public Turn replay(String conversation, String key) {
    return db.replay(conversation, key);
  }

  public void ensureAdmission(Owner owner) {
    db.ensureAdmission(owner);
  }

  public boolean admissionPaused(Owner owner) {
    return db.admissionPaused(owner);
  }

  public boolean active(String conversation) {
    return db.active(conversation);
  }

  public void insertTurn(Turn turn) {
    db.insertTurn(turn);
  }

  public void recordAdmission(Turn turn, String command) {
    db.insertCommand(command, turn.id());
    db.journal(turn.conversationId(), turn.id(), "user", turn.message());
    db.event(turn.id());
  }
}
