package com.financeclaw.app.execution.port;

import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;
import java.util.List;

public interface ExecutionContextGateway {
  Interaction interaction(String id);

  List<Interaction> interactions(String turn);

  java.time.Instant requestClock(String turn);
}
