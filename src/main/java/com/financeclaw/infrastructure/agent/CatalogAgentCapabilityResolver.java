package com.financeclaw.infrastructure.agent;

import com.financeclaw.app.agent.port.AgentAccessGateway;
import com.financeclaw.app.agent.port.AgentCapabilityResolver;
import com.financeclaw.app.agent.port.AgentRepository;
import com.financeclaw.app.agent.port.CredentialAccessGateway;
import com.financeclaw.app.release.ReleaseService;
import com.financeclaw.app.release.port.Credentials;
import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.agent.*;
import com.financeclaw.domain.release.*;
import com.financeclaw.infrastructure.agentscope.RuntimeArtifacts;
import com.financeclaw.infrastructure.agentscope.capability.LocalCapabilityCatalog;
import com.financeclaw.infrastructure.agentscope.capability.McpCapabilityDiscovery;
import com.financeclaw.support.PrivateAgentContracts;
import java.util.*;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Component;

/** 将受控引用编译为固定执行计划；目录只读本地发布，发布探测只访问选中的 MCP 服务。 */
@Component
public final class CatalogAgentCapabilityResolver implements AgentCapabilityResolver {
    private AgentAccessGateway agentAccess = AgentAccessGateway.ownedOnly();
    private CredentialAccessGateway credentialAccess = CredentialAccessGateway.trustedFixture();

    @Autowired
    public void configureCredentialAccess(CredentialAccessGateway access) {
        this.credentialAccess = access;
    }

    @Autowired
    public void configureAgentAccess(AgentAccessGateway access) {
        this.agentAccess = access;
    }

    private final ReleaseService releases;
    private final McpCapabilityDiscovery discovery;
    private final AgentRepository agents;

    public CatalogAgentCapabilityResolver(
            ReleaseService releases, Credentials credentials, AgentRepository agents) {
        this.releases = releases;
        this.discovery = new McpCapabilityDiscovery(credentials);
        this.agents = agents;
    }

    @Override
    public AgentCapabilityCatalog catalog(Owner owner, String baseReleaseHash) {
        var base = releases.require(baseReleaseHash);
        SortedMap<String, PrivateAgentTarget> targets =
                base.scopes().contains(PrivateAgentTarget.TOOL_SCOPE)
                        ? targets(owner)
                        : new TreeMap<>();
        return new AgentCapabilityCatalog(
                base.fallback() == null ? List.of("primary") : List.of("primary", "fallback"),
                options(owner, base, targets).values().stream().toList());
    }

    @Override
    public AgentExecutionPlan resolve(Owner owner, AgentDraft draft) {
        var base = releases.require(draft.baseReleaseHash());
        var selection = draft.composition();
        if (selection == null
                || (selection.modelProfile().equals("fallback") && base.fallback() == null)) {
            throw new Blocked("agent_model_unavailable");
        }
        // 一次 owner 目录查询提供候选及当时固定 hash；仅实际选中的目标读取完整清单。
        SortedMap<String, PrivateAgentTarget> targets =
                selection.capabilities().stream().anyMatch(id -> id.startsWith("agent:"))
                        ? targets(owner)
                        : new TreeMap<>();
        var options = options(owner, base, targets);
        var selected = new TreeSet<String>();
        for (String id : selection.capabilities()) {
            if (id.equals("agent:" + draft.agentId())) {
                throw new Blocked("agent_self_delegation_denied");
            }
            expand(id, options, selected, new HashSet<>());
        }
        if (selected.size() > AgentComposition.MAX_CAPABILITIES) {
            throw new Blocked("agent_capability_unavailable");
        }
        var bindings = new TreeMap<String, CapabilityBinding>();
        LocalCapabilityCatalog.bindings(base).stream()
                .filter(binding -> selected.contains(binding.id()))
                .forEach(binding -> bindings.put(binding.id(), binding));
        for (var remote : base.business().remotes()) {
            if (selected.stream().anyMatch(id -> id.startsWith("mcp:" + remote.service() + "/")))
                credentialAccess.requireCredential(owner, remote.credentialRef());
        }
        discovery
                .discover(owner, base, selected.stream().toList())
                .forEach(binding -> bindings.put(binding.id(), binding));
        for (String id : selected) {
            if (!id.startsWith("agent:")) continue;
            var target = targets.get(id);
            // 不再次读取 current；目录已选定的不可变版本不会因并发发布而被替换。
            var resolved = releases.resolve(target.releaseHash());
            var authorized =
                    agentAccess.requireInvocation(owner, target.agentId(), target.releaseHash());
            PrivateAgentTarget.requireLeaf(authorized.resourceOwner(), resolved, target.profile());
            resolved.requireIdentity(authorized.resourceOwner(), target.agentId());
            if (target.generic()) {
                if (target.profile() == PrivateAgentTarget.Profile.CONTROLLED)
                    PrivateAgentContracts.requireControlled(resolved.plan());
                else PrivateAgentContracts.requireReadOnly(resolved.plan());
                if (!target.scopes().equals(resolved.definition().scopes()))
                    throw new Blocked("agent_plan_invalid");
            }
            LocalCapabilityCatalog.requireCompatible(resolved.plan());
            bindings.put(id, PrivateAgentContracts.binding(target));
        }
        if (!bindings.keySet().equals(selected)) {
            throw new Blocked("agent_capability_unavailable");
        }
        var scopes = new TreeSet<String>();
        bindings.values().forEach(binding -> scopes.addAll(binding.scopes()));
        var remotes = new ArrayList<RemoteEndpoint>();
        for (var remote : base.business().remotes()) {
            var tools =
                    remote.tools().stream()
                            .filter(
                                    name ->
                                            selected.contains(
                                                    "mcp:" + remote.service() + "/" + name))
                            .sorted()
                            .toList();
            if (!tools.isEmpty()) {
                remotes.add(
                        new RemoteEndpoint(
                                remote.service(),
                                remote.url(),
                                remote.allowedHosts(),
                                remote.credentialRef(),
                                remote.external(),
                                remote.tenants(),
                                tools,
                                remote.timeoutMillis()));
            }
        }
        boolean fallback = selection.modelProfile().equals("fallback");
        if (!(fallback ? base.fallback() : base.primary()).name().equals("offline")
                || !fallback
                        && base.fallback() != null
                        && !base.fallback().name().equals("offline"))
            credentialAccess.requireModel(owner);
        var definition =
                new ReleaseDefinition(
                        base.version(),
                        base.agent(),
                        scopes.stream().toList(),
                        fallback ? base.fallback() : base.primary(),
                        fallback ? null : base.fallback(),
                        base.maxModelCalls(),
                        base.maxToolCalls(),
                        new BusinessFeatures(
                                remotes,
                                base.business().skillHash(),
                                base.business().marketJson(),
                                base.business().ziwei()));
        return new AgentExecutionPlan(
                selection.modelProfile(),
                RuntimeArtifacts.hash(),
                selection.capabilities(),
                bindings.values().stream().toList(),
                definition);
    }

    private SortedMap<String, PrivateAgentTarget> targets(Owner owner) {
        var result = new TreeMap<String, PrivateAgentTarget>();
        for (var candidate : agents.listPublishedLeafTargets(owner)) {
            var target =
                    new PrivateAgentTarget(
                            candidate.agentId(), candidate.releaseHash(), candidate.profile());
            if (result.putIfAbsent("agent:" + target.agentId(), target) != null) {
                throw new Blocked("agent_capability_unavailable");
            }
        }
        for (var target : agents.listPublishedReadOnlyTargets(owner)) {
            result.putIfAbsent("agent:" + target.agentId(), target);
        }
        for (var shared : agentAccess.listSharedTargets(owner)) {
            var resolved = releases.resolve(shared.releaseHash());
            try {
                var target =
                        PrivateAgentTarget.fromRelease(
                                shared.resourceOwner(), shared.releaseHash(), resolved);
                if (target.profile() == PrivateAgentTarget.Profile.CONTROLLED)
                    PrivateAgentContracts.requireControlled(resolved.plan());
                else if (target.profile() == PrivateAgentTarget.Profile.READ_ONLY)
                    PrivateAgentContracts.requireReadOnly(resolved.plan());
                result.putIfAbsent("agent:" + target.agentId(), target);
            } catch (Blocked unavailable) {
                if (!"agent_leaf_required".equals(unavailable.getMessage())) throw unavailable;
            }
        }
        return result;
    }

    private static SortedMap<String, CapabilityOption> options(
            Owner owner, ReleaseDefinition base, SortedMap<String, PrivateAgentTarget> targets) {
        var result = new TreeMap<String, CapabilityOption>();
        for (var binding : LocalCapabilityCatalog.bindings(base)) {
            add(
                    result,
                    new CapabilityOption(
                            binding.id(),
                            binding.kind(),
                            binding.name(),
                            binding.scopes(),
                            binding.dependencies()));
        }
        var services = new HashSet<String>();
        for (var remote : base.business().remotes()) {
            if (!services.add(remote.service())) {
                throw new Blocked("agent_capability_unavailable");
            }
            String scope = remote.service().equals("taibu") ? "taibu:read" : "travel:read";
            if (!base.scopes().contains(scope)
                    || (!remote.tenants().isEmpty() && !remote.tenants().contains(owner.tenant()))
                    || (remote.service().equals("taibu") && remote.external())) {
                continue;
            }
            for (String name : remote.tools()) {
                String id = "mcp:" + remote.service() + "/" + name;
                if (!AgentComposition.validId(id)) {
                    throw new Blocked("agent_capability_unavailable");
                }
                add(
                        result,
                        new CapabilityOption(
                                id,
                                "mcp",
                                name,
                                List.of(scope),
                                List.of("function:read_artifact_range")));
            }
        }
        // 结果归档的父侧读取能力必须存在；它不会进入子 Agent 的空工具计划。
        if (base.scopes().contains(PrivateAgentTarget.TOOL_SCOPE)
                && result.containsKey(LocalCapabilityCatalog.ARTIFACT_READER)) {
            for (var target : targets.values()) {
                var binding = PrivateAgentContracts.binding(target);
                // 目录提示不授予权限：父基础发布必须同时允许归档读取及市场数据访问。
                if (!base.scopes().containsAll(binding.scopes())) continue;
                add(
                        result,
                        new CapabilityOption(
                                binding.id(),
                                binding.kind(),
                                binding.name(),
                                binding.scopes(),
                                binding.dependencies()));
            }
        }
        return result;
    }

    private static void add(Map<String, CapabilityOption> result, CapabilityOption option) {
        if (result.putIfAbsent(option.id(), option) != null) {
            throw new Blocked("agent_capability_unavailable");
        }
    }

    private static void expand(
            String id,
            Map<String, CapabilityOption> options,
            Set<String> selected,
            Set<String> visiting) {
        if (selected.contains(id)) return;
        var option = options.get(id);
        if (option == null || !visiting.add(id)) {
            throw new Blocked("agent_capability_unavailable");
        }
        for (String dependency : option.dependencies())
            expand(dependency, options, selected, visiting);
        visiting.remove(id);
        selected.add(id);
    }
}
