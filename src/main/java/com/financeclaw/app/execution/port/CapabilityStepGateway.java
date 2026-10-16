package com.financeclaw.app.execution.port;

import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.StepResult;
import com.financeclaw.domain.Task.*;

public interface CapabilityStepGateway {
  StepResult step(String turn, String namespace, String step);

  void saveStep(String turn, String namespace, String step, String hash, String result);
}
