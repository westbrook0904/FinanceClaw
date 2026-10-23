package com.financeclaw.app.release;

import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.agent.*;
import com.financeclaw.domain.release.ReleaseDefinition;
import com.financeclaw.domain.release.RemoteEndpoint;
import com.financeclaw.support.PayloadDigest;
import com.financeclaw.support.PayloadJson;
import java.nio.charset.StandardCharsets;
import java.util.*;

/** 不信任 resolver 或存储中的投影；发布和读取都复核闭包、契约摘要与基础发布的权限上限。 */
public final class AgentPlanValidator {
    public static final int MAX_CONTRACT_TOTAL_BYTES = 1_048_576;

    private AgentPlanValidator() {}

    public static void validate(AgentExecutionPlan plan, ReleaseDefinition base, String tenant) {
        try {
            verify(plan, base, tenant);
        } catch (RuntimeException invalid) {
            throw new Blocked("agent_plan_invalid");
        }
    }

    private static void verify(AgentExecutionPlan plan, ReleaseDefinition base, String tenant) {
        require(
                plan != null
                        && base != null
                        && CapabilityBinding.validHash(plan.runtimeArtifactHash()));
        var composition = new AgentComposition(plan.modelProfile(), plan.requestedCapabilities());
        require(composition.capabilities().equals(plan.requestedCapabilities()));
        require(
                plan.capabilities() != null
                        && plan.capabilities().size() <= AgentComposition.MAX_CAPABILITIES);
        Map<String, CapabilityBinding> bindings = new HashMap<>();
        Set<String> toolNames = new HashSet<>();
        Set<String> scopes = new HashSet<>();
        int totalBytes = 0;
        for (var binding : plan.capabilities()) {
            binding.validate();
            require(bindings.putIfAbsent(binding.id(), binding) == null);
            require(binding.kind().equals("skill") || toolNames.add(binding.name()));
            scopes.addAll(binding.scopes());
            Object contract = PayloadJson.parse(binding.contractJson());
            require(contract instanceof Map<?, ?>);
            require(PayloadJson.canonical(contract).equals(binding.contractJson()));
            require(PayloadDigest.sha256(binding.contractJson()).equals(binding.contentHash()));
            totalBytes += binding.contractJson().getBytes(StandardCharsets.UTF_8).length;
            require(totalBytes <= MAX_CONTRACT_TOTAL_BYTES);
        }
        if (plan.capabilities().stream().anyMatch(b -> b.kind().equals("skill"))) {
            require(!toolNames.contains("load_skill_through_path"));
        }
        Set<String> complete = new HashSet<>();
        Set<String> visiting = new HashSet<>();
        for (String requested : plan.requestedCapabilities()) {
            visit(requested, bindings, visiting, complete);
        }
        require(complete.equals(bindings.keySet()));
        var actual = plan.definition();
        require(actual != null);
        actual.validate();
        require(
                Objects.equals(actual.version(), base.version())
                        && Objects.equals(actual.agent(), base.agent()));
        require(base.scopes().containsAll(scopes));
        require(
                new HashSet<>(actual.scopes()).equals(scopes)
                        && actual.scopes().size() == scopes.size());
        require(
                actual.maxModelCalls() <= base.maxModelCalls()
                        && actual.maxToolCalls() <= base.maxToolCalls());
        if (plan.modelProfile().equals("primary")) {
            require(
                    actual.primary().equals(base.primary())
                            && Objects.equals(actual.fallback(), base.fallback()));
        } else {
            require(
                    base.fallback() != null
                            && actual.primary().equals(base.fallback())
                            && actual.fallback() == null);
        }
        var business = actual.business();
        var original = base.business();
        require(business != null && original != null);
        require(Objects.equals(business.skillHash(), original.skillHash()));
        require(Objects.equals(business.marketJson(), original.marketJson()));
        require(Objects.equals(business.ziwei(), original.ziwei()));
        Map<String, Set<String>> requestedRemotes = new HashMap<>();
        for (var binding : bindings.values()) {
            if (binding.kind().equals("mcp")) {
                String service = binding.id().substring(4, binding.id().indexOf('/'));
                requestedRemotes
                        .computeIfAbsent(service, ignored -> new HashSet<>())
                        .add(binding.name());
            }
        }
        Map<String, RemoteEndpoint> originals = new HashMap<>();
        for (var remote : original.remotes()) {
            require(originals.putIfAbsent(remote.service(), remote) == null);
        }
        Set<String> seenRemotes = new HashSet<>();
        for (var remote : business.remotes()) {
            require(seenRemotes.add(remote.service()));
            var expectedTools = requestedRemotes.get(remote.service());
            var previous = originals.get(remote.service());
            require(previous != null && expectedTools != null);
            require(
                    remote.tools().size() == expectedTools.size()
                            && new HashSet<>(remote.tools()).equals(expectedTools));
            require(previous.tools().containsAll(expectedTools));
            require(previous.tenants().isEmpty() || previous.tenants().contains(tenant));
            require(
                    remote.equals(
                            new RemoteEndpoint(
                                    previous.service(),
                                    previous.url(),
                                    previous.allowedHosts(),
                                    previous.credentialRef(),
                                    previous.external(),
                                    previous.tenants(),
                                    remote.tools(),
                                    previous.timeoutMillis())));
        }
        require(seenRemotes.equals(requestedRemotes.keySet()));
    }

    private static void visit(
            String id,
            Map<String, CapabilityBinding> bindings,
            Set<String> visiting,
            Set<String> complete) {
        if (complete.contains(id)) {
            return;
        }
        var binding = bindings.get(id);
        require(binding != null && visiting.add(id));
        for (String dependency : binding.dependencies()) {
            visit(dependency, bindings, visiting, complete);
        }
        visiting.remove(id);
        complete.add(id);
    }

    private static void require(boolean condition) {
        if (!condition) {
            throw new Blocked("agent_plan_invalid");
        }
    }
}
