package com.financeclaw.app.execution.port;

import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;

public interface ExecutionReceiptGateway {
  boolean matchesCheckpoint(Claim claim, long version);

  int seal(String attempt, Outcome outcome);

  Receipt receipt(String attempt);

  void insertInteraction(Interaction interaction);
}
