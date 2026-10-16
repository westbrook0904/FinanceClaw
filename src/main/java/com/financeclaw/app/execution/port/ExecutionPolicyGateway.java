package com.financeclaw.app.execution.port;

import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;

public interface ExecutionPolicyGateway {
  void ensureGrant(String conversation, String scopes);

  Grant lockGrant(String conversation);

  void insertPolicy(String turn, long revision, String scopes);

  Policy policy(String turn);

  boolean uncertain(String turn);

  int modelCall(String turn, int max);

  int toolCall(String turn, int max);

  void invalidate(String turn);

  String startedAt(String attempt);
}
