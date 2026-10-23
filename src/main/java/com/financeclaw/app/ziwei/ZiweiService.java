package com.financeclaw.app.ziwei;

import com.financeclaw.app.execution.*;
import com.financeclaw.app.ziwei.port.*;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;
import com.financeclaw.support.PayloadJson;
import com.financeclaw.ziwei.*;
import java.util.*;
import org.springframework.stereotype.Service;

/** 紫微计算编排：根据固定发布策略验证输入，保存确定性步骤回执并通过受治理的发布端口交付结果。 */
@Service
public class ZiweiService {
    private final ExecutionPolicyService policies;
    private final CapabilityStepService steps;
    private final ZiweiInputService inputs;
    private final ZiweiConfiguration configuration;
    private final ZiweiPublication publication;

    public ZiweiService(
            ExecutionPolicyService policies,
            CapabilityStepService steps,
            ZiweiInputService inputs,
            ZiweiConfiguration configuration,
            ZiweiPublication publication) {
        this.policies = policies;
        this.steps = steps;
        this.inputs = inputs;
        this.configuration = configuration;
        this.publication = publication;
    }

    public record Result(boolean suspended, String text) {}

    public void require(Claim claim) {
        inputs.require(claim);
    }

    private void authorize(Claim claim) {
        policies.checkDispatch(claim);
        if (!policies.scopes(claim).contains("ziwei:read")) {
            throw new Blocked("ziwei_denied");
        }
    }

    public Pending pending(
            Claim claim, String tool, String callId, String actionHash, Map<String, Object> input) {
        authorize(claim);
        var review = inputs.inspect(tool, input, inputs.clock(claim, inputs.require(claim)));
        if (review.complete()) {
            throw new Blocked("ziwei_interaction_mismatch");
        }
        return new Pending(
                "input",
                callId,
                tool,
                actionHash,
                PayloadJson.canonical(inputs.answerSchema(tool)),
                review.question());
    }

    public Result resume(
            Claim claim, String tool, String callId, Map<String, Object> input, Object answer) {
        var corrected = inputs.corrected(claim, tool, input, answer);
        policies.consume(claim, false);
        return run(claim, tool, callId, corrected);
    }

    public Result run(Claim claim, String tool, String callId, Map<String, Object> input) {
        authorize(claim);
        var policy = inputs.require(claim);
        byte[] key = configuration.key(policy.keyRef());
        try {
            String binding = ZiweiIdentity.binding(key, policy, claim.turn().id());
            steps.run(claim, "ziwei:key", "binding", binding, () -> Map.of("bound", true));
            var clock = inputs.clock(claim, policy);
            var review = inputs.inspect(tool, input, clock);
            if (!review.complete()) {
                return new Result(true, null);
            }
            var normalized = review.value();
            if (!policy.tzdb().equals(ZiweiDates.tzdbVersion(normalized.timezone()))) {
                throw new Blocked("ziwei_tzdb_changed");
            }
            String fingerprint =
                    ZiweiIdentity.fingerprint(
                            normalized,
                            new Owner(claim.turn().tenant(), claim.turn().subject()),
                            policy,
                            key);
            Object receipt =
                    steps.run(
                            claim,
                            "ziwei:" + callId,
                            "result",
                            List.of(tool, input, fingerprint),
                            () -> {
                                var result =
                                        ZiweiCalculation.calculate(
                                                normalized,
                                                ZiweiDates.Level.forTool(tool),
                                                fingerprint,
                                                policy);
                                var response = new TreeMap<String, Object>();
                                response.put("status", "success");
                                response.put("mode", normalized.mode());
                                response.put("projection", result.projection());
                                response.put(
                                        "time_context",
                                        Map.of(
                                                "request_clock",
                                                clock.requestClock().toString(),
                                                "timezone",
                                                policy.queryTimezone()));
                                response.put(
                                        "artifact",
                                        Map.of(
                                                "artifact_id",
                                                "0".repeat(36),
                                                "hash",
                                                "0".repeat(64),
                                                "bytes",
                                                2097152));
                                if (ZiweiCalculation.bytes(response)
                                        > policy.projectionBytes() + 1024) {
                                    throw new Blocked("ziwei_envelope_budget_exceeded");
                                }
                                var artifact = publication.archive(claim, result.artifact());
                                response.put(
                                        "artifact",
                                        Map.of(
                                                "artifact_id",
                                                artifact.id(),
                                                "hash",
                                                artifact.hash(),
                                                "bytes",
                                                artifact.bytes()));
                                return response;
                            });
            return new Result(false, PayloadJson.canonical(receipt));
        } catch (ZiweiDates.InputError error) {
            return new Result(
                    false,
                    PayloadJson.canonical(
                            Map.of(
                                    "status",
                                    "rejected",
                                    "code",
                                    error.code(),
                                    "field",
                                    error.field(),
                                    "message",
                                    "请缩小查询范围或确认日期；未生成成功盘面。")));
        } finally {
            Arrays.fill(key, (byte) 0);
        }
    }
}
