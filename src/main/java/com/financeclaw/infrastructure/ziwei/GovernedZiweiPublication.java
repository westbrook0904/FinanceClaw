package com.financeclaw.infrastructure.ziwei;

import com.financeclaw.app.ziwei.port.ZiweiPublication;
import com.financeclaw.application.GovernanceService;
import com.financeclaw.domain.Governance.Artifact;
import com.financeclaw.domain.Task.Claim;
import java.util.Map;
import org.springframework.stereotype.Component;

@Component
public class GovernedZiweiPublication implements ZiweiPublication {
  private final GovernanceService governance;

  public GovernedZiweiPublication(GovernanceService governance) {
    this.governance = governance;
  }

  public Artifact archive(Claim claim, Map<String, Object> artifact) {
    return governance.archive(claim, artifact, "ziwei:read");
  }
}
