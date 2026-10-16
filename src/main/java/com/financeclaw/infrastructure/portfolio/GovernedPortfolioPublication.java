package com.financeclaw.infrastructure.portfolio;

import com.financeclaw.app.portfolio.port.PortfolioPublication;
import com.financeclaw.application.GovernanceService;
import com.financeclaw.domain.Governance.Artifact;
import com.financeclaw.domain.Task.Claim;
import org.springframework.stereotype.Component;

/** Joins the step's existing Spring transaction; archive and receipt commit together. */
@Component
public class GovernedPortfolioPublication implements PortfolioPublication {
  private final GovernanceService governance;

  public GovernedPortfolioPublication(GovernanceService governance) {
    this.governance = governance;
  }

  public Artifact archive(Claim claim, Object draft) {
    return governance.archive(claim, draft, "workflow:run");
  }
}
