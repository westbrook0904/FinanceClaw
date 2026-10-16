package com.financeclaw.app.portfolio.port;

import com.financeclaw.domain.Governance.Artifact;
import com.financeclaw.domain.Task.Claim;

public interface PortfolioPublication {
  Artifact archive(Claim claim, Object draft);
}
