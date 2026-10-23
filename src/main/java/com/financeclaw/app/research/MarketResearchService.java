package com.financeclaw.app.research;

import com.financeclaw.app.execution.*;
import com.financeclaw.app.market.MarketDataService;
import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.Task.Claim;
import com.financeclaw.domain.research.ResearchRun;
import com.financeclaw.support.*;
import java.util.*;
import org.springframework.stereotype.Service;

/** 行情研究流程编排：固定输入和步骤身份以支持安全回放，市场数据及最终产物经各自的治理边界提交。 */
@Service
public class MarketResearchService {
    private final ExecutionPolicyService policies;
    private final CapabilityStepService steps;
    private final MarketDataService market;

    public MarketResearchService(
            ExecutionPolicyService policies,
            CapabilityStepService steps,
            MarketDataService market) {
        this.policies = policies;
        this.steps = steps;
        this.market = market;
    }

    public record Execution(
            Claim claim,
            String namespace,
            Map<String, Object> input,
            ResearchRun run,
            String cachedResult) {}

    public Execution begin(Claim claim, String call, Map<String, Object> input) {
        policies.checkDispatch(claim);
        if (!policies.scopes(claim).containsAll(List.of("market:read", "research:run"))) {
            throw new Blocked("research_denied");
        }
        var old = steps.find(claim, "child:" + call, "result");
        if (old != null) {
            if (!old.inputHash().equals(PayloadDigest.sha256(PayloadJson.canonical(input)))) {
                throw new Blocked("child_input_conflict");
            }
            return new Execution(claim, "child:" + call, Map.copyOf(input), null, old.result());
        }
        return new Execution(
                claim,
                "child:" + call,
                Map.copyOf(input),
                new ResearchRun(
                        claim.scope(),
                        call,
                        policies.identity(claim),
                        (List<?>) input.get("symbols")),
                null);
    }

    public void modelCall(Execution execution) {
        execution.run().reserveModelCall();
        policies.consume(execution.claim(), true);
    }

    public Object snapshot(Execution execution, String call, Map<String, Object> input) {
        String symbol = execution.run().reserveSnapshot(input.get("symbol"));
        var snapshot = market.read(execution.claim(), symbol);
        execution.run().recordEvidence(symbol, snapshot);
        return steps.run(
                execution.claim(), execution.namespace(), "tool:" + call, input, () -> snapshot);
    }

    public Object complete(Execution execution, boolean completedState, String summary) {
        policies.checkDispatch(execution.claim());
        var evidence = execution.run().completedEvidence(completedState, summary);
        return steps.run(
                execution.claim(),
                execution.namespace(),
                "result",
                execution.input(),
                () ->
                        Map.of(
                                "outcome",
                                "success",
                                "summary",
                                summary,
                                "evidence",
                                evidence,
                                "analysis_period",
                                execution.input().get("analysis_period"),
                                "limitations",
                                List.of("仅使用发布时冻结的行情快照；不代表实时行情。")));
    }
}
