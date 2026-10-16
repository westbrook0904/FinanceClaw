package com.financeclaw.tools;

import com.financeclaw.application.*;
import com.financeclaw.domain.Task.Claim;
import java.util.function.Supplier;

/** Small transactional checkpoint adapter, used only for local deterministic steps/effects. */
public final class CapabilitySteps {
  private final TaskService tasks;
  private final Claim claim;
  private final String namespace;

  public CapabilitySteps(TaskService tasks, Claim claim, String namespace) {
    this.tasks = tasks;
    this.claim = claim;
    this.namespace = namespace;
  }

  public Object run(String step, Object input, Supplier<Object> localWork) {
    return tasks.localStep(claim, namespace, step, input, localWork);
  }
}
