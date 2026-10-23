package com.financeclaw.infrastructure.task;

import com.financeclaw.app.task.TaskEventHub;
import com.financeclaw.app.task.WorkSignals;
import com.financeclaw.app.task.port.TaskAdmissionGateway;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;
import org.springframework.stereotype.Repository;

@Repository
public class MyBatisTaskAdmissionGateway implements TaskAdmissionGateway {
  private final TaskMapper db;
  private final TaskEventHub events;
  private final WorkSignals signals;

  public MyBatisTaskAdmissionGateway(TaskMapper db, TaskEventHub events, WorkSignals signals) {
    this.signals = signals;
    this.events = events;
    this.db = db;
  }

  public void createConversation(String id, Owner owner) {
    db.insertConversation(id, owner);
  }

  public String lockConversation(String id, Owner owner) {
    return db.getOwnedConversationForUpdate(id, owner);
  }

  public Turn replay(String conversation, String key) {
    return db.getTurnByIdempotencyKey(conversation, key);
  }

  public void ensureAdmission(Owner owner) {
    db.insertAdmissionIfAbsent(owner);
  }

  public boolean admissionPaused(Owner owner) {
    return db.getAdmissionPausedForShare(owner);
  }

  public boolean active(String conversation) {
    return db.existsActiveTurnByConversation(conversation);
  }

  public void insertTurn(Turn turn) {
    db.insertTurn(turn);
  }

  public void recordAdmission(Turn turn, String command) {
    db.insertCommand(command, turn.id());
    db.insertJournalEntryIfAbsent(turn.conversationId(), turn.id(), "user", turn.message());
    events.committed(db.insertTurnEventAndReturn(turn.id()));
    signals.afterCommit(WorkSignals.Queue.COMMAND);
  }
}
