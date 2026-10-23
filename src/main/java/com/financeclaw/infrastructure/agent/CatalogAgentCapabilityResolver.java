package com.financeclaw.infrastructure.agent;

import com.financeclaw.app.agent.port.AgentCapabilityResolver;
import com.financeclaw.app.release.ReleaseService;
import com.financeclaw.app.release.port.Credentials;
import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.agent.*;
import com.financeclaw.domain.release.*;
import com.financeclaw.infrastructure.agentscope.RuntimeArtifacts;
import com.financeclaw.infrastructure.agentscope.capability.LocalCapabilityCatalog;
import com.financeclaw.infrastructure.agentscope.capability.McpCapabilityDiscovery;
import java.util.*;
import org.springframework.stereotype.Component;

/** 将受控引用编译为固定执行计划；目录只读本地发布，发布探测只访问选中的 MCP 服务。 */
@Component
public final class CatalogAgentCapabilityResolver implements AgentCapabilityResolver {
    private final ReleaseService releases;
    private final McpCapabilityDiscovery discovery;

    public CatalogAgentCapabilityResolver(ReleaseService releases, Credentials credentials) {
        this.releases = releases;
        this.discovery = new McpCapabilityDiscovery(credentials);
    }

    @Override
    public AgentCapabilityCatalog catalog(Owner owner, String baseReleaseHash) {
        var base = releases.require(baseReleaseHash);
        return new AgentCapabilityCatalog(
                base.fallback() == null ? List.of("primary") : List.of("primary", "fallback"),
                options(owner, base).values().stream().toList());
    }

    @Override
    public AgentExecutionPlan resolve(Owner owner, AgentDraft draft) {
        var base = releases.require(draft.baseReleaseHash());
        var selection = draft.composition();
        if (selection == null
                || (selection.modelProfile().equals("fallback") && base.fallback() == null)) {
            throw new Blocked("agent_model_unavailable");
        }
        var options = options(owner, base);
        var selected = new TreeSet<String>();
        for (String id : selection.capabilities()) {
            expand(id, options, selected, new HashSet<>());
        }
        if (selected.size() > AgentComposition.MAX_CAPABILITIES) {
            throw new Blocked("agent_capability_unavailable");
        }
        var bindings = new TreeMap<String, CapabilityBinding>();
        LocalCapabilityCatalog.bindings(base).stream()
                .filter(binding -> selected.contains(binding.id()))
                .forEach(binding -> bindings.put(binding.id(), binding));
        discovery
                .discover(owner, base, selected.stream().toList())
                .forEach(binding -> bindings.put(binding.id(), binding));
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

    private static SortedMap<String, CapabilityOption> options(
            Owner owner, ReleaseDefinition base) {
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
