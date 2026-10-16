package com.financeclaw.app.task.port;

import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;

public interface TaskMutationGateway {
  Turn lockTurn(String id);

  Accepted cancelReplay(String turn, String key);

  void recordCancel(String turn, String key, String status, long revision);

  void transition(String turn, String status, String result, String reason);

  void event(String turn);

  void journal(String conversation, String turn, String role, String content);

  void outbox(String turn);

  void advance(String turn);

  void progress(String turn, String kind, String text, String tool);
}
