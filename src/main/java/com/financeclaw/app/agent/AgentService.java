package com.financeclaw.app.agent;

import com.financeclaw.app.agent.port.AgentAccessGateway;
import com.financeclaw.app.agent.port.AgentCapabilityResolver;
import com.financeclaw.app.agent.port.AgentRepository;
import com.financeclaw.app.release.ReleaseService;
import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.Task.Problem;
import com.financeclaw.domain.agent.*;
import com.financeclaw.domain.release.Deployment;
import com.financeclaw.domain.release.DeploymentChange;
import java.util.ArrayList;
import java.util.List;
import java.util.Objects;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionSynchronizationManager;
import org.springframework.transaction.support.TransactionTemplate;

/** Agent 管理与调用目标解析：草稿不进入执行，发布在同一 owner 锁事务中登记清单并切换指针。 */
@Service
public class AgentService {
    private AgentAccessGateway agentAccess = AgentAccessGateway.ownedOnly();
    private boolean sharedAccessConfigured;

    public void configureAgentAccess(AgentAccessGateway access) {
        this.agentAccess = access;
        this.sharedAccessConfigured = true;
    }

    private final AgentRepository repository;
    private final ReleaseService releases;
    private final TransactionTemplate tx;
    private final AgentCapabilityResolver resolver;

    public AgentService(
            AgentRepository repository, ReleaseService releases, TransactionTemplate tx) {
        this(repository, releases, tx, null);
    }

    public AgentService(
            AgentRepository repository,
            ReleaseService releases,
            TransactionTemplate tx,
            AgentCapabilityResolver resolver) {
        this.repository = repository;
        this.releases = releases;
        this.tx = tx;
        this.resolver = resolver;
    }

    /** operator 指定 UUID 是创建幂等身份；重放只能返回最初修订，不能重新选基础发布。 */
    public AgentDraft create(Owner owner, String agentId, String displayName, String systemPrompt) {
        return create(owner, agentId, displayName, systemPrompt, null);
    }

    public AgentDraft create(
            Owner owner,
            String agentId,
            String displayName,
            String systemPrompt,
            AgentComposition composition) {
        AgentDraft.requireCustomId(agentId);
        AgentDraft.requireContent(displayName, systemPrompt);
        if (composition != null) {
            requireResolver();
        }
        return tx.execute(
                status -> {
                    boolean created =
                            repository.insertIfAbsent(
                                    new AgentDefinition(
                                            agentId, owner.tenant(), owner.subject(), 1));
                    requireOwned(repository.lockOwned(owner, agentId));
                    if (!created) {
                        var initial = requireDraft(repository.draft(owner, agentId, 1));
                        if (!initial.displayName().equals(displayName)
                                || !initial.systemPrompt().equals(systemPrompt)
                                || !Objects.equals(initial.composition(), composition)) {
                            throw new Problem(409, "agent_id_conflict");
                        }
                        return initial;
                    }
                    String base = releases.currentHash();
                    var draft =
                            new AgentDraft(
                                    agentId, 1, displayName, systemPrompt, base, composition);
                    repository.insertDraft(draft);
                    return draft;
                });
    }

    public AgentDraft get(Owner owner, String id) {
        if (AgentDraft.BUILTIN_AGENT.equals(id)) {
            return new AgentDraft(
                    id, 0, "Finance Agent", AgentDraft.BUILTIN_PROMPT, releases.currentHash());
        }
        AgentDraft.requireCustomId(id);
        var agent = requireOwned(repository.owned(owner, id));
        return requireDraft(repository.draft(owner, id, agent.draftRevision()));
    }

    public List<AgentDefinition> list(Owner owner) {
        var result = new ArrayList<AgentDefinition>();
        result.add(new AgentDefinition(AgentDraft.BUILTIN_AGENT, null, null, 0));
        result.addAll(repository.list(owner));
        return List.copyOf(result);
    }

    public AgentDraft update(
            Owner owner, String id, long expectedDraftRevision, String displayName, String prompt) {
        return update(owner, id, expectedDraftRevision, displayName, prompt, null, true);
    }

    public AgentDraft update(
            Owner owner,
            String id,
            long expectedDraftRevision,
            String displayName,
            String prompt,
            AgentComposition composition) {
        return update(owner, id, expectedDraftRevision, displayName, prompt, composition, false);
    }

    private AgentDraft update(
            Owner owner,
            String id,
            long expectedDraftRevision,
            String displayName,
            String prompt,
            AgentComposition composition,
            boolean preserveComposition) {
        AgentDraft.requireCustomId(id);
        AgentDraft.requireContent(displayName, prompt);
        requireRevision(expectedDraftRevision);
        if (composition != null) {
            requireResolver();
        }
        return tx.execute(
                status -> {
                    var agent = requireOwned(repository.lockOwned(owner, id));
                    requireCurrentDraft(agent, expectedDraftRevision);
                    var previous = requireDraft(repository.draft(owner, id, expectedDraftRevision));
                    if (!repository.advanceDraft(owner, id, expectedDraftRevision)) {
                        throw new Problem(409, "agent_draft_revision_changed");
                    }
                    var next =
                            new AgentDraft(
                                    id,
                                    expectedDraftRevision + 1,
                                    displayName,
                                    prompt,
                                    previous.baseReleaseHash(),
                                    preserveComposition ? previous.composition() : composition);
                    repository.insertDraft(next);
                    return next;
                });
    }

    public Deployment publish(
            Owner owner, String id, long expectedDraftRevision, long expectedDeploymentRevision) {
        AgentDraft.requireCustomId(id);
        requireRevision(expectedDraftRevision);
        var initialAgent = requireOwned(repository.owned(owner, id));
        requireCurrentDraft(initialAgent, expectedDraftRevision);
        var initialDraft = requireDraft(repository.draft(owner, id, expectedDraftRevision));
        if (initialDraft.composition() != null) {
            // 不能通过加入调用方事务隐藏远程发现；发现完成后重新锁行并验证精确修订。
            if (TransactionSynchronizationManager.isActualTransactionActive()) {
                throw new Problem(409, "agent_publish_transaction_active");
            }
            var plan = requireResolver().resolve(owner, initialDraft);
            if (plan == null
                    || !initialDraft.composition().modelProfile().equals(plan.modelProfile())
                    || !initialDraft
                            .composition()
                            .capabilities()
                            .equals(plan.requestedCapabilities())) {
                throw new Blocked("agent_plan_invalid");
            }
            return tx.execute(
                    status -> {
                        var agent = requireOwned(repository.lockOwned(owner, id));
                        requireCurrentDraft(agent, expectedDraftRevision);
                        var draft =
                                requireDraft(repository.draft(owner, id, expectedDraftRevision));
                        if (!draft.equals(initialDraft)) {
                            throw new Problem(409, "agent_draft_revision_changed");
                        }
                        String hash =
                                releases.registerComposedAgent(
                                        ComposedAgentReleaseManifest.from(agent, draft, plan));
                        return releases.activateForAgent(id, hash, expectedDeploymentRevision);
                    });
        }
        return tx.execute(
                status -> {
                    var agent = requireOwned(repository.lockOwned(owner, id));
                    requireCurrentDraft(agent, expectedDraftRevision);
                    var draft = requireDraft(repository.draft(owner, id, expectedDraftRevision));
                    String hash = releases.registerAgent(AgentReleaseManifest.from(agent, draft));
                    // 与清单注册共享外层事务；CAS 或回执写入失败时不能留下半次发布。
                    return releases.activateForAgent(id, hash, expectedDeploymentRevision);
                });
    }

    public AgentCapabilityCatalog catalog(Owner owner, String id) {
        var draft = get(owner, id);
        return requireResolver().catalog(owner, draft.baseReleaseHash());
    }

    private AgentCapabilityResolver requireResolver() {
        if (resolver == null) {
            throw new Problem(409, "agent_capabilities_unavailable");
        }
        return resolver;
    }

    public ResolvedTarget resolveTarget(Owner owner, String id) {
        if (AgentDraft.BUILTIN_AGENT.equals(id)) {
            String hash = releases.currentHash();
            releases.resolve(hash).requireIdentity(owner, id);
            return new ResolvedTarget(id, hash);
        }
        AgentDraft.requireCustomId(id);
        if (!sharedAccessConfigured) requireOwned(repository.owned(owner, id));
        var deployment = releases.currentForAgent(id);
        if (deployment == null) {
            if (repository.owned(owner, id) == null) throw new Problem(404, "not_found");
            throw new Problem(409, "agent_not_published");
        }
        com.financeclaw.app.agent.port.AgentAccessGateway.Authorization authorization;
        try {
            authorization = agentAccess.requireInvocation(owner, id, deployment.releaseHash());
        } catch (Blocked denied) {
            if ("agent_access_denied".equals(denied.getMessage()))
                throw new Problem(404, "not_found");
            throw denied;
        }
        releases.resolve(deployment.releaseHash())
                .requireIdentity(authorization.resourceOwner(), id);
        return new ResolvedTarget(id, deployment.releaseHash());
    }

    public List<DeploymentChange> history(Owner owner, String id, long after, int limit) {
        if (AgentDraft.BUILTIN_AGENT.equals(id)) {
            return releases.history(after, limit);
        }
        AgentDraft.requireCustomId(id);
        requireOwned(repository.owned(owner, id));
        return releases.historyForAgent(id, after, limit);
    }

    private static AgentDefinition requireOwned(AgentDefinition agent) {
        if (agent == null) {
            throw new Problem(404, "not_found");
        }
        return agent;
    }

    private static AgentDraft requireDraft(AgentDraft draft) {
        if (draft == null) {
            throw new Problem(409, "agent_draft_unavailable");
        }
        return draft;
    }

    private static void requireRevision(long revision) {
        if (revision < 1 || revision == Long.MAX_VALUE) {
            throw new Problem(422, "invalid_draft_revision");
        }
    }

    private static void requireCurrentDraft(AgentDefinition agent, long expected) {
        if (agent.draftRevision() != expected) {
            throw new Problem(409, "agent_draft_revision_changed");
        }
    }
}
