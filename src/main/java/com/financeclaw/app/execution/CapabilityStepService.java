package com.financeclaw.app.execution;

import com.financeclaw.app.execution.port.CapabilityStepGateway;
import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.StepResult;
import com.financeclaw.domain.Task.Claim;
import com.financeclaw.support.PayloadDigest;
import com.financeclaw.support.PayloadJson;
import java.util.function.Supplier;
import org.springframework.stereotype.Service;

/** 确定性能力步骤的幂等边界：输入摘要与结果一起提交，相同键只允许相同输入回放。事务内禁止模型和远程调用。 */
@Service
public class CapabilityStepService {
    private final CapabilityStepGateway steps;
    private final ExecutionLeaseService leases;
    private final ExecutionPolicyService policies;

    public CapabilityStepService(
            CapabilityStepGateway steps,
            ExecutionLeaseService leases,
            ExecutionPolicyService policies) {
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

    /** 对纯本地确定性步骤执行输入摘要检查和原子持久化，重复调用返回首次结果；远程副作用不适用此边界。 */
    public Object run(
            Claim claim, String namespace, String step, Object input, Supplier<Object> localWork) {
        String hash = PayloadDigest.sha256(PayloadJson.canonical(input));
        return leases.fenced(
                claim,
                () -> {
                    policies.checkPolicy(claim);
                    var old = steps.step(claim.turn().id(), namespace, step);
                    if (old != null) {
                        if (!old.inputHash().equals(hash)) {
                            throw new Blocked("step_input_conflict");
                        }
                        return PayloadJson.parse(old.result());
                    }
                    Object result = localWork.get();
                    steps.saveStep(
                            claim.turn().id(),
                            namespace,
                            step,
                            hash,
                            PayloadJson.canonical(result));
                    return result;
                });
    }
}
