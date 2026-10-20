package com.financeclaw.app.artifact;

import com.financeclaw.app.artifact.port.ArtifactGateway;
import com.financeclaw.app.execution.ExecutionLeaseService;
import com.financeclaw.app.execution.ExecutionPolicyService;
import com.financeclaw.app.release.ReleaseService;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;
import com.financeclaw.support.ArtifactPayload;
import com.financeclaw.support.PayloadJson;
import java.util.UUID;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionTemplate;

/** Archive under an execution fence and read under current owner, privacy and grant authority. */
@Service
public class ArtifactService {
  private final ArtifactGateway db;
  private final ExecutionLeaseService leases;
  private final ExecutionPolicyService policy;
  private final ReleaseService releases;
  private final TransactionTemplate tx;

  public ArtifactService(
      ArtifactGateway db,
      ExecutionLeaseService leases,
      ExecutionPolicyService policy,
      ReleaseService releases,
      TransactionTemplate tx) {
    this.db = db;
    this.leases = leases;
    this.policy = policy;
    this.releases = releases;
    this.tx = tx;
  }

  public Artifact archive(Claim claim, Object payload) {
    var encoded = ArtifactPayload.encode(payload);
    return leases.fenced(
        claim,
        () -> {
          policy.checkPolicy(claim);
          var turn = claim.turn();
          var artifact =
              new Artifact(
                  UUID.randomUUID().toString(),
                  turn.id(),
                  turn.conversationId(),
                  turn.tenant(),
                  turn.subject(),
                  turn.releaseHash(),
                  encoded.hash(),
                  encoded.json(),
                  encoded.bytes());
          db.insertArtifact(artifact);
          return artifact;
        });
  }

  public Artifact archive(Claim claim, Object payload, String requiredScope) {
    return leases.fenced(
        claim,
        () -> {
          policy.checkPolicy(claim);
          if (!PayloadJson.scopes(db.policy(claim.turn().id()).scopes()).contains(requiredScope))
            throw new Blocked("artifact_scope_denied");
          var artifact = archive(claim, payload);
          db.setArtifactScope(artifact.id(), requiredScope);
          return artifact;
        });
  }

  public Page read(Owner owner, String id, int start, int limit) {
    return read(owner, id, start, limit, "");
  }

  public Page read(Owner owner, String id, int start, int limit, String path) {
    return tx.execute(
        status -> {
          var artifact = db.artifact(id);
          if (artifact == null
              || !artifact.tenant().equals(owner.tenant())
              || !artifact.subject().equals(owner.subject())) throw new Problem(404, "not_found");
          // Match execution's owner-before-grant lock order when privacy governance is enabled.
          if ("ziwei:read".equals(db.artifactScope(id)))
            policy.checkArtifactPrivacy(db.turn(artifact.turnId()));
          var grant = db.lockGrant(artifact.conversationId());
          if (grant == null || !PayloadJson.scopes(grant.scopes()).contains("artifact:read"))
            throw new Problem(403, "artifact_denied");
          if (!PayloadJson.scopes(grant.scopes()).contains(db.artifactScope(id)))
            throw new Problem(403, "artifact_scope_denied");
          try {
            releases.require(artifact.releaseHash());
          } catch (Blocked error) {
            throw new Problem(409, error.getMessage());
          }
          return ArtifactPayload.read(artifact, start, limit, path);
        });
  }
}
