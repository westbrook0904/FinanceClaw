package com.financeclaw.infrastructure.agentscope.capability;

import com.financeclaw.app.execution.ExecutionPolicyService;
import com.financeclaw.app.market.MarketDataService;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.Claim;
import com.financeclaw.domain.research.ResearchRun;
import com.financeclaw.infrastructure.agentscope.ResumeInputs;
import com.financeclaw.support.PayloadJson;
import io.agentscope.core.message.*;
import io.agentscope.core.tool.ToolCallParam;
import java.time.Duration;
import java.util.*;
import reactor.core.publisher.Mono;

/**
 * 注册市场快照和市场研究工具，并声明各自需要的 scope。 研究缺少证券或期间时生成持久输入交互，恢复从已保存调用合并答案并重新预留工具预算。实际数据读取与子 Agent
 * 执行仍通过对应受治理用例。
 */
public final class MarketToolContribution implements ToolContribution {
    private final ExecutionPolicyService policies;
    private final MarketDataService market;
    private final Claim claim;
    private final MarketResearchAgent research;

    public MarketToolContribution(
            ExecutionPolicyService policies,
            MarketDataService market,
            MarketResearchAgent research,
            Claim claim) {
        this.policies = policies;
        this.market = market;
        this.claim = claim;
        this.research = research;
    }

    @Override
    public void contribute(Registrar registrar) {
        registrar.tool(
                "market_snapshot",
                MarketToolSchemas.SNAPSHOT,
                "market:read",
                p ->
                        Mono.fromCallable(
                                () ->
                                        ToolResultBlock.text(
                                                PayloadJson.canonical(
                                                        market.read(
                                                                claim,
                                                                (String)
                                                                        p.getInput()
                                                                                .get("symbol"))))));
        registrar.tool(
                "market_research",
                MarketToolSchemas.RESEARCH,
                "research:run",
                research::run,
                new ResearchInteraction());
    }

    private final class ResearchInteraction implements InteractionHandler {
        @Override
        public Pending pending(ToolUseBlock call) {
            return new Pending(
                    "input",
                    call.getId(),
                    call.getName(),
                    ResumeInputs.actionHash(call),
                    PayloadJson.canonical(MarketToolSchemas.ANSWER),
                    "请提供研究证券代码和分析期间");
        }

        @Override
        public Msg resume(ToolUseBlock call, Object answer) {
            var input = new TreeMap<>(call.getInput());
            ((Map<?, ?>) answer).forEach((key, value) -> input.put((String) key, value));
            if (ResearchRun.missing(input)) {
                throw new Blocked("research_scope_missing");
            }
            policies.consume(claim, false);
            var result =
                    research.run(ToolCallParam.builder().toolUseBlock(call).input(input).build())
                            .block(Duration.ofSeconds(20));
            return ResumeInputs.result(call, result);
        }
    }
}
