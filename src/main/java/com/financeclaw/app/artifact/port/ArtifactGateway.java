package com.financeclaw.app.artifact.port;

import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.Turn;

/** Persistence required to archive and authorize a stored Artifact. */
public interface ArtifactGateway {
  Artifact artifact(String id);

  int insertArtifact(Artifact artifact);

  String artifactScope(String id);

  int setArtifactScope(String id, String scope);

  Policy policy(String turn);

  Turn turn(String id);

  Grant lockGrant(String conversation);
}
