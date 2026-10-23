package com.financeclaw.app.execution.port;

import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;
import java.util.List;

public interface CommandGateway {
  Command nextCommand();

  Command lockCommand(String id);

  Command lockTurnCommand(String turn);

  void claim(String id, String attempt, String worker, long lease);

  void insertAttempt(Command command);

  /** Null means the command is fenced; otherwise return the locked Turn's cancellation state. */
  Boolean renew(Claim claim, long lease);

  void finishCommand(String command);

  List<String> expired();
}
