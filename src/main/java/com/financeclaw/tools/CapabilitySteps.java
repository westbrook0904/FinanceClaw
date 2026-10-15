package com.financeclaw.tools;

import com.financeclaw.agent.ResumeInputs;
import com.financeclaw.application.*;
import com.financeclaw.domain.Governance.Blocked;
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
    String hash = GovernanceService.digest(ResumeInputs.canonical(input));
    return tasks.fenced(
        claim,
        () -> {
          tasks.checkPolicy(claim);
          var old = tasks.governance().step(claim.turn().id(), namespace, step);
          if (old != null) {
            if (!old.inputHash().equals(hash)) throw new Blocked("step_input_conflict");
            return GovernanceService.parse(old.result());
          }
          Object result = localWork.get();
          tasks
              .governance()
              .saveStep(claim.turn().id(), namespace, step, hash, ResumeInputs.canonical(result));
          return result;
        });
  }
}
