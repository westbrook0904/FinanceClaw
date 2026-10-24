package com.financeclaw.infrastructure.agentscope.capability;

import com.financeclaw.app.execution.*;
import com.financeclaw.app.market.MarketDataService;
import com.financeclaw.app.portfolio.PortfolioReviewService;
import com.financeclaw.app.research.MarketResearchService;
import com.financeclaw.domain.Task.Claim;
import com.financeclaw.domain.agent.AgentExecutionPlan;
import com.financeclaw.domain.agent.PrivateAgentTarget;
import com.financeclaw.infrastructure.state.RuntimeMapper;
import com.financeclaw.support.PrivateAgentContracts;
import io.agentscope.core.model.Model;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Component;

/** Assembly for the migrated market, portfolio and Ziwei capabilities. */
@Component
public class BusinessCapabilities {
    private final MarketDataService market;
    private final PortfolioReviewService portfolio;
    private final MarketResearchService research;
    private final ExecutionPolicyService policies;
    private final RuntimeMapper states;
    private final com.financeclaw.app.ziwei.ZiweiService ziwei;
    private final PrivateAgentDelegation privateAgents;

    public BusinessCapabilities(
            MarketDataService market,
            PortfolioReviewService portfolio,
            MarketResearchService research,
            ExecutionPolicyService policies,
            RuntimeMapper states,
            com.financeclaw.app.ziwei.ZiweiService ziwei) {
        this(market, portfolio, research, policies, states, ziwei, null);
    }

    @Autowired
    public BusinessCapabilities(
            MarketDataService market,
            PortfolioReviewService portfolio,
            MarketResearchService research,
            ExecutionPolicyService policies,
            RuntimeMapper states,
            com.financeclaw.app.ziwei.ZiweiService ziwei,
            PrivateAgentDelegation privateAgents) {
        this.market = market;
        this.portfolio = portfolio;
        this.research = research;
        this.policies = policies;
        this.states = states;
        this.ziwei = ziwei;
        this.privateAgents = privateAgents;
    }

    public void contribute(
            Claim claim, Model model, boolean workflow, ToolContribution.Registrar registrar) {
        new MarketToolContribution(
                        policies,
                        market,
                        new MarketResearchAgent(research, states, claim, model),
                        claim)
                .contribute(registrar);
        new PortfolioToolContribution(portfolio, claim, workflow).contribute(registrar);
    }

    public void ziwei(Claim claim, ToolContribution.Registrar registrar) {
        new ZiweiToolContribution(ziwei, claim).contribute(registrar);
    }

    /** 只有父发布明确选择的 agent binding 才注册；叶子自身从不经过本工具装配。 */
    public void privateAgents(
            Claim claim, AgentExecutionPlan plan, ToolContribution.Registrar registrar) {
        if (plan == null) return;
        for (var binding : plan.capabilities()) {
            if (!binding.kind().equals("agent")) continue;
            PrivateAgentContracts.from(binding);
            if (privateAgents == null)
                throw new com.financeclaw.domain.Governance.Blocked("capability_unavailable");
            registrar.tool(
                    binding.name(),
                    PrivateAgentTarget.DESCRIPTION,
                    PrivateAgentTarget.INPUT_SCHEMA,
                    PrivateAgentTarget.TOOL_SCOPE,
                    param -> privateAgents.run(claim, binding, param),
                    null);
        }
    }

    public Model portfolioDriver(Claim claim) {
        return new PortfolioDriver(portfolio, claim);
    }
}
