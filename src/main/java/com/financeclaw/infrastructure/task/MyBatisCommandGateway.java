package com.financeclaw.infrastructure.task;

import com.financeclaw.app.execution.port.CommandGateway;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;
import java.util.List;
import org.springframework.stereotype.Repository;

@Repository
public class MyBatisCommandGateway implements CommandGateway {
  private final TaskMapper db;

  public MyBatisCommandGateway(TaskMapper db) {
    this.db = db;
  }

  public Command nextCommand() {
    return db.nextCommand();
  }

  public Command lockCommand(String id) {
    return db.lockCommand(id);
  }

  public Command lockTurnCommand(String turn) {
    return db.lockTurnCommand(turn);
  }

  public void claim(String id, String attempt, String worker, long lease) {
    db.claim(id, attempt, worker, lease);
  }

  public void insertAttempt(Command command) {
    db.insertAttempt(command);
  }

  public void renew(String command, long lease) {
    db.renew(command, lease);
  }

  public void finishCommand(String command) {
    db.finishCommand(command);
  }

  public List<String> expired() {
    return db.expired();
  }
}
