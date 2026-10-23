package com.financeclaw.app.research;

import com.financeclaw.app.execution.*;
import com.financeclaw.app.market.MarketDataService;
import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.Task.Claim;
import com.financeclaw.domain.research.ResearchRun;
import com.financeclaw.support.PayloadJson;
import java.util.*;
import org.springframework.stereotype.Service;

/** 行情研究流程编排：固定输入和步骤身份以支持安全回放，市场数据及最终产物经各自的治理边界提交。 */
@Service
public class MarketResearchService {
    private final ExecutionPolicyService policies;
    private final ChildInvocationService children;
    private final MarketDataService market;

    public MarketResearchService(
            ExecutionPolicyService policies,
            ChildInvocationService children,
            MarketDataService market) {
        this.policies = policies;
        this.children = children;
        this.market = market;
    }

    public record Execution(
            Claim claim,
            String namespace,
            Map<String, Object> input,
            ResearchRun run,
            String cachedResult,
            ChildInvocationService.Invocation invocation) {}

    public Execution begin(Claim claim, String call, Map<String, Object> input) {
        var invocation = children.begin(claim, call, input);
        if (invocation.cachedResult() != null) {
            return new Execution(
                    claim,
                    invocation.namespace(),
                    Map.copyOf(input),
                    null,
                    invocation.cachedResult(),
                    invocation);
        }
        var run =
                new ResearchRun(
                        claim.scope(),
                        call,
                        policies.identity(claim),
                        (List<?>) input.get("symbols"));
        // SDK 可能已持久化工具结果或最终答复而业务 result 尚未提交。证据从唯一成功回执恢复，
        // 不能依赖 SDK 再次执行工具，也不能从模型自己的文本重建证据。
        for (String result : invocation.snapshotResults()) {
            var snapshot = PayloadJson.object(result);
            if (!(snapshot.get("symbol") instanceof String symbol)) {
                throw new Blocked("child_invocation_invalid");
            }
            run.recordEvidence(symbol, snapshot);
        }
        return new Execution(
                claim, invocation.namespace(), Map.copyOf(input), run, null, invocation);
    }

    public void modelCall(Execution execution) {
        children.reserveModel(execution.invocation());
    }

    /** SDK 状态读取与保存复用同一个父调用栅栏，检查点本身不授予执行或读取权限。 */
    public <T> T checkpoint(Execution execution, java.util.function.Supplier<T> operation) {
        return children.checkpoint(execution.invocation(), operation);
    }

    @SuppressWarnings("unchecked")
    public Object snapshot(Execution execution, String call, Map<String, Object> input) {
        // 先校验证券范围；持久子/根额度只在缓存未命中且实际读取时提交。
        String symbol = execution.run().validateSnapshot(input.get("symbol"));
        var snapshot =
                (Map<String, Object>)
                        children.snapshot(
                                execution.invocation(),
                                call,
                                input,
                                () -> market.read(execution.claim(), symbol));
        // 恢复运行时 evidence 起初为空，缓存回执同样必须补回证据，才能完整校验研究结果。
        execution.run().recordEvidence(symbol, snapshot);
        return snapshot;
    }

    public Object complete(Execution execution, boolean completedState, String summary) {
        var evidence = execution.run().completedEvidence(completedState, summary);
        return children.complete(
                execution.invocation(),
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
