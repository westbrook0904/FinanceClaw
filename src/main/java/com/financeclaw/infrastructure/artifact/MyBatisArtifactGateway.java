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
    return db.artifact(id);
  }

  public int insertArtifact(Artifact artifact) {
    return db.insertArtifact(artifact);
  }

  public String artifactScope(String id) {
    return db.artifactScope(id);
  }

  public int setArtifactScope(String id, String scope) {
    return db.setArtifactScope(id, scope);
  }

  public Policy policy(String turn) {
    return db.policy(turn);
  }

  public Turn turn(String id) {
    return turns.turn(id);
  }

  public Grant lockGrant(String conversation) {
    return db.lockGrant(conversation);
  }
}
