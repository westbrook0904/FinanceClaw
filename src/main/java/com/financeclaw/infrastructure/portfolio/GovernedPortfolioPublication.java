package com.financeclaw.infrastructure.portfolio;

import com.financeclaw.app.artifact.ArtifactService;
import com.financeclaw.app.portfolio.port.PortfolioPublication;
import com.financeclaw.domain.Governance.Artifact;
import com.financeclaw.domain.Task.Claim;
import org.springframework.stereotype.Component;

/** Joins the step's existing Spring transaction; archive and receipt commit together. */
@Component
public class GovernedPortfolioPublication implements PortfolioPublication {
  private final ArtifactService artifacts;

  public GovernedPortfolioPublication(ArtifactService artifacts) {
    this.artifacts = artifacts;
  }

  public Artifact archive(Claim claim, Object draft) {
    return artifacts.archive(claim, draft, "workflow:run");
  }
}
