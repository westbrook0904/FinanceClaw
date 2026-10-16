package com.financeclaw.infrastructure.agentscope.capability;

import com.financeclaw.app.execution.*;
import com.financeclaw.app.market.MarketDataService;
import com.financeclaw.app.portfolio.PortfolioReviewService;
import com.financeclaw.app.research.MarketResearchService;
import com.financeclaw.domain.Task.Claim;
import com.financeclaw.infrastructure.state.RuntimeMapper;
import io.agentscope.core.model.Model;
import org.springframework.stereotype.Component;

/** Assembly for the migrated market and portfolio capabilities. */
@Component
public class BusinessCapabilities {
  private final MarketDataService market;
  private final PortfolioReviewService portfolio;
  private final MarketResearchService research;
  private final ExecutionPolicyService policies;
  private final ExecutionLeaseService leases;
  private final RuntimeMapper states;

  public BusinessCapabilities(
      MarketDataService market,
      PortfolioReviewService portfolio,
      MarketResearchService research,
      ExecutionPolicyService policies,
      ExecutionLeaseService leases,
      RuntimeMapper states) {
    this.market = market;
    this.portfolio = portfolio;
    this.research = research;
    this.policies = policies;
    this.leases = leases;
    this.states = states;
  }

  public void contribute(
      Claim claim, Model model, boolean workflow, ToolContribution.Registrar registrar) {
    new MarketToolContribution(
            policies,
            market,
            new MarketResearchAgent(research, leases, states, claim, model),
            claim)
        .contribute(registrar);
    new PortfolioToolContribution(portfolio, claim, workflow).contribute(registrar);
  }

  public Model portfolioDriver(Claim claim) {
    return new PortfolioDriver(portfolio, claim);
  }
}
