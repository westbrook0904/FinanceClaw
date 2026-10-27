package com.financeclaw.infrastructure.agent;

import com.financeclaw.app.agent.port.CredentialAccessGateway;
import com.financeclaw.app.agent.port.ResourceAdministration;
import com.financeclaw.app.release.ReleaseService;
import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.Task.Problem;
import com.financeclaw.domain.agent.AgentDraft;
import com.financeclaw.domain.authorization.CredentialResource;
import com.financeclaw.domain.authorization.ResourceGrant;
import com.financeclaw.domain.authorization.ResourceIdentity;
import java.util.List;
import java.util.Objects;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionSynchronizationManager;
import org.springframework.transaction.support.TransactionTemplate;

/** 当前资源授权事实；publisher owner 和执行 caller 不互相替代。 */
@Service
public class ResourceAccessService implements ResourceAdministration, CredentialAccessGateway {
    private final ResourceAccessMapper mapper;
    private final AgentMapper agents;
    private final ReleaseService releases;
    private final TransactionTemplate tx;
    private final ServerCredentialCatalog credentials;

    public ResourceAccessService(
            ResourceAccessMapper mapper,
            AgentMapper agents,
            ReleaseService releases,
            TransactionTemplate tx,
            ServerCredentialCatalog credentials) {
        this.mapper = mapper;
        this.agents = agents;
        this.releases = releases;
        this.tx = tx;
        this.credentials = credentials;
    }

    /**
     * 每次调用重新核验资源归属与精确发布授权。返回的 resourceOwner 仅用于校验发布身份，不能替换 Turn、记忆或产物的 caller；解析结果只供本次校验链复用，不缓存授权。
     */
    @Override
    public Authorization requireInvocation(Owner caller, String agentId, String releaseHash) {
        if (caller == null || releaseHash == null || !releaseHash.matches("[0-9a-f]{64}"))
            throw denied();
        if (AgentDraft.BUILTIN_AGENT.equals(agentId)) return new Authorization(caller, 0);
        if (!AgentDraft.isCustomId(agentId)) throw denied();
        Owner owner = resourceOwner(agentId);
        var resolved = releases.resolve(releaseHash);
        resolved.requireIdentity(owner, agentId);
        if (owner.equals(caller)) return new Authorization(owner, 0, resolved);
        var grant = invocationGrant("agent", agentId, caller);
        if (!active(grant, owner) || !releaseHash.equals(grant.releaseHash())) throw denied();
        return new Authorization(owner, grant.revision(), resolved);
    }

    @Override
    public Owner resourceOwner(String agentId) {
        if (!AgentDraft.isCustomId(agentId)) throw denied();
        // 本地提交期间持共享锁，归档须等事务完成；事务外的网络调用预检只读当前事实。
        var owner =
                TransactionSynchronizationManager.isActualTransactionActive()
                        ? mapper.agentOwnerForShare(agentId)
                        : mapper.agentOwner(agentId);
        if (owner == null) throw denied();
        return owner;
    }

    @Override
    public List<SharedTarget> listSharedTargets(Owner caller) {
        var rows = bounded(mapper.sharedAgents(caller));
        return rows.stream()
                .map(
                        g ->
                                new SharedTarget(
                                        g.resourceId(),
                                        g.releaseHash(),
                                        g.resourceOwner(),
                                        g.revision()))
                .toList();
    }

    @Override
    public void requireModel(Owner caller) {
        requireReference(caller, ServerCredentialCatalog.MODEL);
    }

    @Override
    public void requireCredential(Owner caller, String reference) {
        if (reference == null || reference.isBlank()) return;
        requireReference(caller, reference);
    }

    private void requireReference(Owner caller, String reference) {
        var resource = credentials.reference(reference);
        if (resource == null) {
            if (credentials.permitsLegacy(caller)) return;
            throw new Blocked("credential_access_denied");
        }
        if (resource.owner().equals(caller)) return;
        var grant = invocationGrant("credential", resource.resourceId(), caller);
        if (!active(grant, resource.owner()) || !resource.bindingHash().equals(grant.bindingHash()))
            throw new Blocked("credential_access_denied");
    }

    /** 与资源归属使用同一事务边界：本地效果提交前保护 grant，远程调用前不持锁等待网络。 */
    private ResourceGrant invocationGrant(String kind, String id, Owner caller) {
        return TransactionSynchronizationManager.isActualTransactionActive()
                ? mapper.grantForShare(kind, id, caller)
                : mapper.grant(kind, id, caller);
    }

    public List<CredentialResource> credentialResources(Owner owner) {
        return credentials.owned(owner);
    }

    /** owner 行锁与草稿修订 CAS 共同防止并发发布、更新和归档覆盖彼此。 */
    public void archive(Owner owner, String agentId, long expectedRevision) {
        AgentDraft.requireCustomId(agentId);
        if (expectedRevision < 1 || expectedRevision == Long.MAX_VALUE)
            throw new Problem(422, "invalid_draft_revision");
        tx.executeWithoutResult(
                status -> {
                    if (agents.getOwnedAgentForUpdate(owner, agentId) == null) throw missing();
                    if (mapper.archive(owner, agentId, expectedRevision) != 1)
                        throw new Problem(409, "agent_draft_revision_changed");
                });
    }

    /** expectedRevision=0 创建，否则更新精确修订；撤销也追加修订而不删除事实。 Agent 授权先锁定归属；授权修改与审计追加在同一事务中完成，任一步失败全部回滚。 */
    public ResourceGrant change(
            Owner actor,
            String kind,
            String id,
            Owner grantee,
            String releaseHash,
            long expectedRevision,
            boolean enabled) {
        validate(kind, grantee, expectedRevision);
        return tx.execute(
                status -> {
                    requireResourceOwner(actor, kind, id, true);
                    if ("agent".equals(kind)) {
                        if (releaseHash == null || !releaseHash.matches("[0-9a-f]{64}"))
                            throw new Problem(422, "invalid_release_hash");
                        releases.resolve(releaseHash).requireIdentity(actor, id);
                    } else if (releaseHash != null) throw new Problem(422, "invalid_release_hash");
                    if (actor.equals(grantee)) throw new Problem(422, "self_grant_not_required");
                    var grant =
                            new ResourceGrant(
                                    kind,
                                    id,
                                    actor.tenant(),
                                    actor.subject(),
                                    grantee.tenant(),
                                    grantee.subject(),
                                    releaseHash,
                                    "credential".equals(kind)
                                            ? credentials.resource(id).bindingHash()
                                            : null,
                                    expectedRevision + 1,
                                    enabled);
                    int changed =
                            expectedRevision == 0
                                    ? mapper.insert(grant)
                                    : mapper.update(grant, expectedRevision);
                    if (changed != 1) throw new Problem(409, "resource_grant_revision_changed");
                    if (mapper.audit(grant) != 1)
                        throw new IllegalStateException("Grant audit was not appended");
                    return grant;
                });
    }

    public List<ResourceGrant> grants(Owner owner, String kind, String id) {
        requireResourceOwner(owner, kind, id, false);
        return bounded(mapper.grants(kind, id, owner));
    }

    public List<ResourceGrant> history(
            Owner owner, String kind, String id, Owner grantee, long after, int limit) {
        validate(kind, grantee, after);
        if (limit < 1 || limit > 200) throw new Problem(422, "invalid_page_limit");
        requireResourceOwner(owner, kind, id, false);
        return mapper.history(kind, id, owner, grantee, after, limit);
    }

    private void requireResourceOwner(Owner owner, String kind, String id, boolean lock) {
        if ("agent".equals(kind)) {
            AgentDraft.requireCustomId(id);
            if ((lock ? agents.getOwnedAgentForUpdate(owner, id) : agents.getOwnedAgent(owner, id))
                    == null) throw missing();
        } else if ("credential".equals(kind)) {
            var resource = credentials.resource(id);
            if (resource == null || !resource.owner().equals(owner)) throw missing();
        } else throw new Problem(422, "invalid_resource_kind");
    }

    private static void validate(String kind, Owner grantee, long revision) {
        if (!("agent".equals(kind) || "credential".equals(kind))
                || grantee == null
                || !ResourceIdentity.valid(grantee.tenant())
                || !ResourceIdentity.valid(grantee.subject())
                || revision < 0
                || revision == Long.MAX_VALUE) throw new Problem(422, "invalid_resource_grant");
    }

    private static boolean active(ResourceGrant grant, Owner owner) {
        return grant != null && grant.enabled() && Objects.equals(grant.resourceOwner(), owner);
    }

    private static <T> List<T> bounded(List<T> values) {
        if (values.size() > 1000) throw new Problem(409, "resource_catalog_too_large");
        return List.copyOf(values);
    }

    private static Blocked denied() {
        return new Blocked("agent_access_denied");
    }

    private static Problem missing() {
        return new Problem(404, "not_found");
    }
}
