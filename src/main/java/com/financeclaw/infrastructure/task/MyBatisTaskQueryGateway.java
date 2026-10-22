package com.financeclaw.infrastructure.task;

import com.financeclaw.app.task.port.TaskQueryGateway;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;
import java.util.List;
import org.springframework.stereotype.Repository;

@Repository
public class MyBatisTaskQueryGateway implements TaskQueryGateway {
  private final TaskMapper db;

  public MyBatisTaskQueryGateway(TaskMapper db) {
    this.db = db;
  }

  public String ownedConversation(String id, Owner owner) {
    return db.ownedConversation(id, owner);
  }

  public Turn ownedTurn(Owner owner, String conversation, String turn) {
    return db.ownedTurn(owner, conversation, turn);
  }

  public Turn turn(String id) {
    return db.turn(id);
  }

  public List<Journal> messages(String conversation, long after, int limit) {
    return db.messages(conversation, after, limit);
  }

  public List<Event> events(String turn, long after) {
    return db.events(turn, after);
  }

  public List<Turn> history(String conversation, String release, long revision) {
    return db.history(conversation, release, revision);
  }
}
