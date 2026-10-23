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

/** 产物归档与读取边界：产物绑定用户、发布版本和读取权限，按完整载荷摘要校验；读取时重新验证权限和遗忘状态。 */
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
                    return persistArtifact(claim, encoded);
                });
    }

    public Artifact archive(Claim claim, Object payload, String requiredScope) {
        return leases.fenced(
                claim,
                () -> {
                    policy.checkPolicy(claim);
                    if (!PayloadJson.scopes(db.policy(claim.turn().id()).scopes())
                            .contains(requiredScope)) {
                        throw new Blocked("artifact_scope_denied");
                    }
                    // 权限校验和产物插入沿用当前栅栏事务，无需再次调用同一授权及租约检查。
                    var artifact = persistArtifact(claim, ArtifactPayload.encode(payload));
                    db.setArtifactScope(artifact.id(), requiredScope);
                    return artifact;
                });
    }

    /** 仅由已持有执行栅栏且通过授权的入口调用，不自行开启事务或重复读取权限。 */
    private Artifact persistArtifact(Claim claim, ArtifactPayload encoded) {
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
                            || !artifact.subject().equals(owner.subject())) {
                        throw new Problem(404, "not_found");
                    }
                    // 先取隐私用户锁、后取授权锁，与执行端顺序一致，避免遗忘和读取形成反向等待。
                    if ("ziwei:read".equals(db.artifactScope(id))) {
                        policy.checkArtifactPrivacy(db.turn(artifact.turnId()));
                    }
                    var grant = db.lockGrant(artifact.conversationId());
                    if (grant == null
                            || !PayloadJson.scopes(grant.scopes()).contains("artifact:read")) {
                        throw new Problem(403, "artifact_denied");
                    }
                    if (!PayloadJson.scopes(grant.scopes()).contains(db.artifactScope(id))) {
                        throw new Problem(403, "artifact_scope_denied");
                    }
                    try {
                        releases.require(artifact.releaseHash());
                    } catch (Blocked error) {
                        throw new Problem(409, error.getMessage());
                    }
                    return ArtifactPayload.read(artifact, start, limit, path);
                });
    }
}
