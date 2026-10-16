package com.financeclaw.app.task.port;

import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;

public interface TaskAdmissionGateway {
  void createConversation(String id, Owner owner);

  String lockConversation(String id, Owner owner);

  Turn replay(String conversation, String key);

  void ensureAdmission(Owner owner);

  boolean admissionPaused(Owner owner);

  boolean active(String conversation);

  void insertTurn(Turn turn);

  void recordAdmission(Turn turn, String command);
}
