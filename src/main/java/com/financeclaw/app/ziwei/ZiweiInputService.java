package com.financeclaw.app.ziwei;

import com.financeclaw.app.execution.port.ExecutionContextGateway;
import com.financeclaw.app.ziwei.port.*;
import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.Task.*;
import com.financeclaw.domain.release.ZiweiPolicy;
import com.financeclaw.ziwei.*;
import java.util.Map;
import org.springframework.stereotype.Service;

/** 紫微输入治理：按发布版本绑定的规则解析用户输入，对缺失字段生成可恢复交互，不在校验阶段执行外部写入。 */
@Service
public class ZiweiInputService {
    private final ZiweiConfiguration configuration;
    private final ExecutionContextGateway context;
    private final ZiweiInputRules inputs;

    public ZiweiInputService(
            ZiweiConfiguration configuration,
            ExecutionContextGateway context,
            ZiweiInputRules inputs) {
        this.configuration = configuration;
        this.context = context;
        this.inputs = inputs;
    }

    public ZiweiPolicy require(Claim claim) {
        var policy = configuration.policy(claim.turn().releaseHash());
        if (policy == null) {
            throw new Blocked("ziwei_disabled");
        }
        policy.verifyRuntime();
        return policy;
    }

    public ZiweiDates.QueryClock clock(Claim claim, ZiweiPolicy policy) {
        return new ZiweiDates.QueryClock(
                context.requestClock(claim.turn().id()), policy.queryTimezone());
    }

    public ZiweiInputs.Review inspect(
            String tool, Map<String, Object> input, ZiweiDates.QueryClock clock) {
        return inputs.inspect(tool, input, clock);
    }

    public Map<String, Object> answerSchema(String tool) {
        return inputs.answerSchema(tool);
    }

    public Map<String, Object> corrected(
            Claim claim, String tool, Map<String, Object> original, Object answer) {
        var corrected = inputs.corrected(tool, original, answer);
        if (!inputs.inspect(tool, corrected, clock(claim, require(claim))).complete()) {
            throw new Blocked("ziwei_input_incomplete");
        }
        return corrected;
    }

    public void validateAnswer(
            Claim claim, String tool, Map<String, Object> original, Object answer) {
        var policy = configuration.policy(claim.turn().releaseHash());
        if (policy == null) {
            throw new Problem(409, "ziwei_disabled");
        }
        try {
            var corrected = inputs.corrected(tool, original, answer);
            if (!inputs.inspect(tool, corrected, clock(claim, policy)).complete()) {
                throw new Problem(422, "ziwei_input_incomplete");
            }
        } catch (IllegalArgumentException e) {
            throw new Problem(422, "invalid_answer");
        }
    }
}
