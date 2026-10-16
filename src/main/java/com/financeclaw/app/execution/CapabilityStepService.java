package com.financeclaw.app.execution;

import com.financeclaw.app.execution.port.CapabilityStepGateway;
import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.StepResult;
import com.financeclaw.domain.Task.Claim;
import com.financeclaw.support.PayloadDigest;
import com.financeclaw.support.PayloadJson;
import java.util.function.Supplier;
import org.springframework.stereotype.Service;

/** Local deterministic work only; no model or remote calls inside this transaction. */
@Service
public class CapabilityStepService {
  private final CapabilityStepGateway steps;
  private final ExecutionLeaseService leases;
  private final ExecutionPolicyService policies;

  public CapabilityStepService(
      CapabilityStepGateway steps, ExecutionLeaseService leases, ExecutionPolicyService policies) {
    this.steps = steps;
    this.leases = leases;
    this.policies = policies;
  }

  public StepResult find(Claim claim, String namespace, String step) {
    return leases.fenced(
        claim,
        () -> {
          policies.checkPolicy(claim);
          return steps.step(claim.turn().id(), namespace, step);
        });
  }

  public Object run(
      Claim claim, String namespace, String step, Object input, Supplier<Object> localWork) {
    String hash = PayloadDigest.sha256(PayloadJson.canonical(input));
    return leases.fenced(
        claim,
        () -> {
          policies.checkPolicy(claim);
          var old = steps.step(claim.turn().id(), namespace, step);
          if (old != null) {
            if (!old.inputHash().equals(hash)) throw new Blocked("step_input_conflict");
            return PayloadJson.parse(old.result());
          }
          Object result = localWork.get();
          steps.saveStep(claim.turn().id(), namespace, step, hash, PayloadJson.canonical(result));
          return result;
        });
  }
}
