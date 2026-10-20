package com.financeclaw.infrastructure.ziwei;

import com.financeclaw.app.artifact.ArtifactService;
import com.financeclaw.app.ziwei.port.ZiweiPublication;
import com.financeclaw.domain.Governance.Artifact;
import com.financeclaw.domain.Task.Claim;
import java.util.Map;
import org.springframework.stereotype.Component;

@Component
public class GovernedZiweiPublication implements ZiweiPublication {
  private final ArtifactService artifacts;

  public GovernedZiweiPublication(ArtifactService artifacts) {
    this.artifacts = artifacts;
  }

  public Artifact archive(Claim claim, Map<String, Object> artifact) {
    return artifacts.archive(claim, artifact, "ziwei:read");
  }
}
