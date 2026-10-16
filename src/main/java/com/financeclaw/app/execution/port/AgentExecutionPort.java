package com.financeclaw.app.execution.port;

import com.financeclaw.domain.Task.*;

/** execute returns only after the native call and its resources have stopped. */
public interface AgentExecutionPort {
  Outcome execute(Claim claim);

  void interrupt(Claim claim);
}
