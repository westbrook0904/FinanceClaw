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
    return db.getNextQueuedCommandForUpdateSkipLocked();
  }

  public Command lockCommand(String id) {
    return db.getCommandForUpdate(id);
  }

  public Command lockTurnCommand(String turn) {
    return db.getCommandByTurnIdForUpdate(turn);
  }

  public void claim(String id, String attempt, String worker, long lease) {
    db.updateCommandClaim(id, attempt, worker, lease);
  }

  public void insertAttempt(Command command) {
    db.insertAttempt(command);
  }

  public Boolean renew(Claim claim, long lease) {
    return db.updateCommandLeaseAndGetCancellation(claim, lease);
  }

  public void finishCommand(String command) {
    db.updateCommandDone(command);
  }

  public List<String> expired() {
    return db.listExpiredCommandIds();
  }
}
