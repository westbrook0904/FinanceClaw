package com.financeclaw.app.task.port;

import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;
import java.util.List;

public interface TaskQueryGateway {
  String ownedConversation(String id, Owner owner);

  Turn turn(String id);

  List<Journal> messages(String conversation, long after, int limit);

  List<Event> events(String turn, long after);

  List<Turn> history(String conversation, String release, long revision);
}
