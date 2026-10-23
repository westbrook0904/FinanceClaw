package com.financeclaw.infrastructure.artifact;

import com.financeclaw.app.artifact.port.ArtifactGateway;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.Turn;
import com.financeclaw.infrastructure.governance.GovernanceMapper;
import com.financeclaw.infrastructure.task.TaskMapper;
import org.springframework.stereotype.Repository;

@Repository
public class MyBatisArtifactGateway implements ArtifactGateway {
  private final GovernanceMapper db;
  private final TaskMapper turns;

  public MyBatisArtifactGateway(GovernanceMapper db, TaskMapper turns) {
    this.db = db;
    this.turns = turns;
  }

  public Artifact artifact(String id) {
    return db.getArtifactById(id);
  }

  public int insertArtifact(Artifact artifact) {
    return db.insertArtifact(artifact);
  }

  public String artifactScope(String id) {
    return db.getArtifactScope(id);
  }

  public int setArtifactScope(String id, String scope) {
    return db.updateArtifactScope(id, scope);
  }

  public Policy policy(String turn) {
    return db.getTurnPolicy(turn);
  }

  public Turn turn(String id) {
    return turns.getTurnById(id);
  }

  public Grant lockGrant(String conversation) {
    return db.getGrantForUpdate(conversation);
  }
}
