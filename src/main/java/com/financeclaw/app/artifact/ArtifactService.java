package com.financeclaw.app.artifact;

import com.financeclaw.app.agent.port.AgentAccessGateway;
import com.financeclaw.app.artifact.port.ArtifactGateway;
import com.financeclaw.app.execution.ExecutionLeaseService;
import com.financeclaw.app.execution.ExecutionPolicyService;
import com.financeclaw.app.release.ReleaseService;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;
import com.financeclaw.support.ArtifactPayload;
import com.financeclaw.support.PayloadJson;
import com.financeclaw.support.ScopeRequirements;
import java.util.*;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionTemplate;

/** 产物归档与读取边界：产物绑定用户、发布版本和读取权限，按完整载荷摘要校验；读取时重新验证权限和遗忘状态。 */
@Service
public class ArtifactService {
    private AgentAccessGateway agentAccess = AgentAccessGateway.ownedOnly();

    public void configureAgentAccess(AgentAccessGateway access) {
        this.agentAccess = access;
    }

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
                    if (!ScopeRequirements.allowed(
                            PayloadJson.scopes(db.policy(claim.turn().id()).scopes()),
                            requiredScope)) {
                        throw new Blocked("artifact_scope_denied");
                    }
                    // 权限校验和产物插入沿用当前栅栏事务，无需再次调用同一授权及租约检查。
                    var artifact = persistArtifact(claim, ArtifactPayload.encode(payload));
                    db.setArtifactScope(artifact.id(), requiredScope);
                    return artifact;
                });
    }

    /** 在当前执行栅栏中归档完整工具结果；后续每次读取均检查该 Turn 的遗忘状态和原工具权限。 正文和权限标记一次插入，只有事务成功后调用方才能用引用替换模型上下文中的原文。 */
    public Artifact archiveToolResult(Claim claim, Object payload, String requiredScope) {
        return leases.fenced(
                claim,
                () -> {
                    var checked = policy.checkPolicy(claim);
                    var scope = requiredScope == null ? "artifact:read" : requiredScope;
                    if (!ScopeRequirements.allowed(PayloadJson.scopes(checked.scopes()), scope)) {
                        throw new Blocked("artifact_scope_denied");
                    }
                    var artifact = artifact(claim, ArtifactPayload.encode(payload));
                    if (db.insertToolResultArtifact(artifact, scope) != 1) {
                        throw new Blocked("artifact_archive_failed");
                    }
                    return artifact;
                });
    }

    /** 仅由已持有执行栅栏且通过授权的入口调用，不自行开启事务或重复读取权限。 */
    private Artifact persistArtifact(Claim claim, ArtifactPayload encoded) {
        var artifact = artifact(claim, encoded);
        db.insertArtifact(artifact);
        return artifact;
    }

    private Artifact artifact(Claim claim, ArtifactPayload encoded) {
        var turn = claim.turn();
        return new Artifact(
                UUID.randomUUID().toString(),
                turn.id(),
                turn.conversationId(),
                turn.tenant(),
                turn.subject(),
                turn.releaseHash(),
                encoded.hash(),
                encoded.json(),
                encoded.bytes());
    }

    public Page read(Owner owner, String id, int start, int limit) {
        return read(owner, id, start, limit, "");
    }

    public Page read(Owner owner, String id, int start, int limit, String path) {
        return tx.execute(
                status -> ArtifactPayload.read(authorized(owner, id), start, limit, path));
    }

    /** start/maxBytes 均是归档 JSON 的 UTF-8 字节偏移，不接受服务器路径或任意文件名。 */
    public ArtifactPayload.Range readRange(Owner owner, String id, int start, int maxBytes) {
        return tx.execute(
                status -> ArtifactPayload.readRange(authorized(owner, id), start, maxBytes));
    }

    /** 组合 Agent 的读取还受当前执行计划和授权限制；HTTP owner 读取继续使用原有入口。 */
    public Page readForExecution(
            Claim claim,
            List<String> effectiveScopes,
            String id,
            int start,
            int limit,
            String path) {
        return leases.fenced(
                claim,
                () ->
                        ArtifactPayload.read(
                                authorizedForExecution(claim, effectiveScopes, id),
                                start,
                                limit,
                                path));
    }

    public ArtifactPayload.Range readRangeForExecution(
            Claim claim, List<String> effectiveScopes, String id, int start, int maxBytes) {
        return leases.fenced(
                claim,
                () ->
                        ArtifactPayload.readRange(
                                authorizedForExecution(claim, effectiveScopes, id),
                                start,
                                maxBytes));
    }

    private Artifact authorizedForExecution(Claim claim, List<String> effectiveScopes, String id) {
        var owner = new Owner(claim.turn().tenant(), claim.turn().subject());
        var artifact = owned(owner, id);
        var access = access(artifact);
        // 同 owner 隐私锁先于两个会话授权锁；按 ID 排序避免 A 读 B、B 读 A 时锁反序。
        policy.checkArtifactPrivacy(claim.turn());
        checkPrivacy(artifact, access);
        var conversations =
                new TreeSet<>(List.of(claim.turn().conversationId(), artifact.conversationId()));
        var grants = new HashMap<String, Grant>();
        for (String conversation : conversations) {
            grants.put(conversation, db.lockGrant(conversation));
        }
        var current = policy.checkPolicy(claim);
        var resolved = releases.resolve(claim.turn().releaseHash());
        resolved.requireIdentity(
                agentAccess
                        .requireInvocation(
                                owner, claim.turn().agentId(), claim.turn().releaseHash())
                        .resourceOwner(),
                claim.turn().agentId());
        var fixedScopes = resolved.definition().scopes();
        var currentScopes = PayloadJson.scopes(current.scopes());
        requireExecutionScope(access.requiredScope(), effectiveScopes, fixedScopes, currentScopes);
        if (!access.privacyBound()) {
            requireExecutionScope("artifact:read", effectiveScopes, fixedScopes, currentScopes);
        }
        checkGrant(access, grants.get(artifact.conversationId()));
        checkRelease(artifact);
        return artifact;
    }

    private static void requireExecutionScope(
            String scope, List<String> effective, List<String> fixed, List<String> current) {
        if (effective == null
                || !ScopeRequirements.allowed(effective, scope)
                || !ScopeRequirements.allowed(fixed, scope)
                || !ScopeRequirements.allowed(current, scope)) {
            throw new Blocked("artifact_scope_denied");
        }
    }

    /** 返回经授权和完整性校验的完整 JSON 文件；控制器负责 attachment 响应，数据库始终是正文权威。 */
    public ArtifactPayload.Document download(Owner owner, String id) {
        return tx.execute(status -> ArtifactPayload.document(authorized(owner, id)));
    }

    /** 必须在读取事务内调用：用户隐私锁先于授权锁，与执行及遗忘路径保持相同顺序。 */
    private Artifact authorized(Owner owner, String id) {
        var artifact = owned(owner, id);
        var access = access(artifact);
        checkPrivacy(artifact, access);
        var grant = db.lockGrant(artifact.conversationId());
        checkGrant(access, grant);
        checkRelease(artifact);
        return artifact;
    }

    private Artifact owned(Owner owner, String id) {
        var artifact = db.artifact(id);
        if (artifact == null
                || !artifact.tenant().equals(owner.tenant())
                || !artifact.subject().equals(owner.subject())) {
            throw new Problem(404, "not_found");
        }
        return artifact;
    }

    private ArtifactGateway.Access access(Artifact artifact) {
        var access = db.artifactAccess(artifact.id());
        if (access == null || access.requiredScope() == null) {
            throw new Problem(409, "artifact_corrupt");
        }
        return access;
    }

    private void checkPrivacy(Artifact artifact, ArtifactGateway.Access access) {
        if (access.privacyBound()
                || ScopeRequirements.decode(access.requiredScope()).contains("ziwei:read")) {
            var turn = db.turn(artifact.turnId());
            if (turn == null
                    || !turn.tenant().equals(artifact.tenant())
                    || !turn.subject().equals(artifact.subject())) {
                throw new Problem(409, "artifact_corrupt");
            }
            policy.checkArtifactPrivacy(turn);
        }
    }

    private static void checkGrant(ArtifactGateway.Access access, Grant grant) {
        // 工具文件只是原工具回执的另一种载体，不能额外要求 artifact:read 把 skill-only 等合法调用截断。
        // 普通业务产物继续要求 artifact:read 和其自身 required scope，范围接口不改变这条权限边界。
        if (grant == null
                || (!access.privacyBound()
                        && !PayloadJson.scopes(grant.scopes()).contains("artifact:read"))) {
            throw new Problem(403, "artifact_denied");
        }
        if (!ScopeRequirements.allowed(
                PayloadJson.scopes(grant.scopes()), access.requiredScope())) {
            throw new Problem(403, "artifact_scope_denied");
        }
    }

    private void checkRelease(Artifact artifact) {
        try {
            var turn = db.turn(artifact.turnId());
            if (turn == null) throw new Problem(409, "artifact_corrupt");
            com.financeclaw.app.agent.AgentResultAccess.require(
                    agentAccess,
                    releases,
                    new Owner(artifact.tenant(), artifact.subject()),
                    turn.agentId(),
                    artifact.releaseHash());
        } catch (Blocked error) {
            throw new Problem(409, error.getMessage());
        }
    }
}
