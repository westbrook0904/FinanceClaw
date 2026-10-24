package com.financeclaw.app.release;

import static org.assertj.core.api.Assertions.*;

import com.financeclaw.domain.agent.*;
import com.financeclaw.domain.release.*;
import com.financeclaw.support.PayloadDigest;
import com.financeclaw.support.PrivateAgentContracts;
import java.util.*;
import org.junit.jupiter.api.Test;

class AgentPlanValidatorTest {
    private static final String CONTRACT = "{\"schema\":{\"type\":\"object\"}}";
    private static final String ARTIFACT = "a".repeat(64);

    @Test
    void compositionSortsIdentifiersButRejectsDuplicatesAndUnknownProfiles() {
        assertThat(
                        new AgentComposition("primary", List.of("skill:demo", "function:lookup"))
                                .capabilities())
                .containsExactly("function:lookup", "skill:demo");
        assertThat(new AgentComposition("primary", List.of()).capabilities()).isEmpty();
        assertThatThrownBy(
                        () ->
                                new AgentComposition(
                                        "primary", List.of("function:lookup", "function:lookup")))
                .hasMessage("invalid_agent_composition");
        for (String id :
                List.of("function:../lookup", "mcp:lookup", "unknown:lookup", "function:")) {
            assertThatThrownBy(() -> new AgentComposition("primary", List.of(id)))
                    .hasMessage("invalid_agent_composition");
        }
        assertThatThrownBy(() -> new AgentComposition("another", List.of()))
                .hasMessage("invalid_agent_composition");
    }

    @Test
    void acceptsExactDependencyClosureAndExplicitEmptySelection() {
        var read = binding("function:lookup", "lookup", List.of("catalog:read"), List.of());
        var helper = binding("function:helper", "helper", List.of(), List.of("function:lookup"));
        AgentPlanValidator.validate(
                plan(List.of("function:helper"), List.of(helper, read)), base(), "tenant");
        AgentPlanValidator.validate(plan(List.of(), List.of()), base(), "tenant");
    }

    @Test
    void rejectsMissingCyclicOrUnrequestedDependenciesAndDuplicateBindings() {
        var leaf = binding("function:lookup", "lookup", List.of(), List.of());
        var missing = binding("function:helper", "helper", List.of(), List.of("function:missing"));
        var first = binding("function:first", "first", List.of(), List.of("function:second"));
        var second = binding("function:second", "second", List.of(), List.of("function:first"));
        for (var invalid :
                List.of(
                        plan(List.of("function:helper"), List.of(missing)),
                        plan(List.of("function:first"), List.of(first, second)),
                        plan(List.of(), List.of(leaf)),
                        plan(List.of("function:lookup"), List.of(leaf, leaf)),
                        plan(List.of("function:missing"), List.of()))) {
            assertInvalid(invalid);
        }
    }

    @Test
    void rejectsContractTamperingNonCanonicalJsonAndOversizeContracts() {
        for (String contract :
                List.of(
                        "{ \"schema\":{} }",
                        "[]",
                        "{\"x\":\"" + "x".repeat(CapabilityBinding.MAX_CONTRACT_BYTES) + "\"}")) {
            var binding =
                    new CapabilityBinding(
                            "function:lookup",
                            "function",
                            "lookup",
                            List.of(),
                            List.of(),
                            contract,
                            PayloadDigest.sha256(contract));
            assertInvalid(plan(List.of(binding.id()), List.of(binding)));
        }
        var tampered =
                new CapabilityBinding(
                        "function:lookup",
                        "function",
                        "lookup",
                        List.of(),
                        List.of(),
                        CONTRACT,
                        "0".repeat(64));
        assertInvalid(plan(List.of(tampered.id()), List.of(tampered)));
    }

    @Test
    void rejectsCapabilityAndProjectionScopeExpansionAndUnneededPermissions() {
        var unknownScope = binding("function:lookup", "lookup", List.of("root:all"), List.of());
        assertInvalid(plan(List.of(unknownScope.id()), List.of(unknownScope)));
        var empty = plan(List.of(), List.of());
        assertInvalid(withDefinition(empty, base()));
        var low = empty.definition();
        assertInvalid(
                withDefinition(
                        empty,
                        new ReleaseDefinition(
                                low.version(),
                                low.agent(),
                                List.of(),
                                low.primary(),
                                low.fallback(),
                                13,
                                low.maxToolCalls(),
                                low.business())));
        assertInvalid(
                withDefinition(
                        empty,
                        new ReleaseDefinition(
                                low.version(),
                                low.agent(),
                                List.of(),
                                low.primary(),
                                low.fallback(),
                                low.maxModelCalls(),
                                33,
                                low.business())));
    }

    @Test
    void fallbackMustExistAndCannotIntroduceAReplacementModelOrNewBusinessData() {
        var empty = plan(List.of(), List.of());
        assertInvalid(
                new AgentExecutionPlan(
                        "fallback", ARTIFACT, List.of(), List.of(), empty.definition()));
        var low = empty.definition();
        var changed =
                new BusinessFeatures(
                        List.of(), low.business().skillHash(), "{\"changed\":true}", null);
        assertInvalid(
                withDefinition(
                        empty,
                        new ReleaseDefinition(
                                low.version(),
                                low.agent(),
                                List.of(),
                                low.primary(),
                                null,
                                12,
                                32,
                                changed)));
        var fallback = new ModelProfile("offline", "", 32768, 65536, 1024, 256, false);
        var base =
                new ReleaseDefinition(
                        "base",
                        "finance_agent",
                        ReleaseDefinition.SCOPES,
                        ReleaseDefinition.OFFLINE,
                        fallback,
                        12,
                        32,
                        base().business());
        var projection =
                new ReleaseDefinition(
                        "base",
                        "finance_agent",
                        List.of(),
                        fallback,
                        null,
                        12,
                        32,
                        base.business());
        AgentPlanValidator.validate(
                new AgentExecutionPlan("fallback", ARTIFACT, List.of(), List.of(), projection),
                base,
                "tenant");
        assertThatThrownBy(
                        () ->
                                AgentPlanValidator.validate(
                                        new AgentExecutionPlan(
                                                "primary",
                                                ARTIFACT,
                                                List.of(),
                                                List.of(),
                                                projection),
                                        base,
                                        "tenant"))
                .hasMessage("agent_plan_invalid");
    }

    @Test
    void mcpProjectionMustMatchExactlySelectedToolsAndApprovedEndpoint() {
        var remote =
                new RemoteEndpoint(
                        "hotel",
                        "http://localhost/mcp",
                        List.of("localhost"),
                        "",
                        false,
                        List.of("tenant"),
                        List.of("lookup", "detail"),
                        1000);
        var base =
                new ReleaseDefinition(
                        "base",
                        "finance_agent",
                        ReleaseDefinition.SCOPES,
                        ReleaseDefinition.OFFLINE,
                        null,
                        12,
                        32,
                        new BusinessFeatures(List.of(remote), "skills", "{}", null));
        var binding =
                new CapabilityBinding(
                        "mcp:hotel/lookup",
                        "mcp",
                        "lookup",
                        List.of("travel:read"),
                        List.of(),
                        CONTRACT,
                        PayloadDigest.sha256(CONTRACT));
        var narrowed =
                new RemoteEndpoint(
                        remote.service(),
                        remote.url(),
                        remote.allowedHosts(),
                        remote.credentialRef(),
                        remote.external(),
                        remote.tenants(),
                        List.of("lookup"),
                        remote.timeoutMillis());
        var projection =
                new ReleaseDefinition(
                        "base",
                        "finance_agent",
                        List.of("travel:read"),
                        base.primary(),
                        null,
                        12,
                        32,
                        new BusinessFeatures(List.of(narrowed), "skills", "{}", null));
        var plan =
                new AgentExecutionPlan(
                        "primary", ARTIFACT, List.of(binding.id()), List.of(binding), projection);
        AgentPlanValidator.validate(plan, base, "tenant");
        assertThatThrownBy(() -> AgentPlanValidator.validate(plan, base, "other"))
                .hasMessage("agent_plan_invalid");
        assertThatThrownBy(
                        () ->
                                AgentPlanValidator.validate(
                                        withDefinition(
                                                plan,
                                                new ReleaseDefinition(
                                                        "base",
                                                        "finance_agent",
                                                        List.of("travel:read"),
                                                        base.primary(),
                                                        null,
                                                        12,
                                                        32,
                                                        base.business())),
                                        base,
                                        "tenant"))
                .hasMessage("agent_plan_invalid");
        var function = binding("function:lookup", "lookup", List.of(), List.of());
        var collision =
                new AgentExecutionPlan(
                        "primary",
                        ARTIFACT,
                        List.of(function.id(), binding.id()),
                        List.of(binding, function),
                        projection);
        assertThatThrownBy(() -> AgentPlanValidator.validate(collision, base, "tenant"))
                .hasMessage("agent_plan_invalid");
    }

    @Test
    void runtimeArtifactAndRequestedOrderingAreValidated() {
        var empty = plan(List.of(), List.of());
        assertInvalid(
                new AgentExecutionPlan(
                        "primary", "missing", List.of(), List.of(), empty.definition()));
        var a = binding("function:a", "a", List.of(), List.of());
        var b = binding("function:b", "b", List.of(), List.of());
        assertInvalid(plan(List.of(b.id(), a.id()), List.of(a, b)));
    }

    @Test
    void privateTargetsAreValidatedAsFixedContractsWithoutResolvingAnotherAgent() {
        var target =
                PrivateAgentContracts.binding(
                        "11e8e638-0689-40df-99ed-b22de07c1172", "b".repeat(64));
        var reader =
                binding(
                        "function:read_artifact_range",
                        "read_artifact_range",
                        List.of("artifact:read"),
                        List.of());
        AgentPlanValidator.validate(
                plan(List.of(target.id()), List.of(target, reader)), base(), "tenant");
        assertInvalid(plan(List.of(target.id()), List.of(target)));
        var contract =
                new LinkedHashMap<>(
                        com.financeclaw.support.PayloadJson.object(target.contractJson()));
        contract.put("maxDepth", 2);
        String json = com.financeclaw.support.PayloadJson.canonical(contract);
        var recursive =
                new CapabilityBinding(
                        target.id(),
                        target.kind(),
                        target.name(),
                        target.scopes(),
                        target.dependencies(),
                        json,
                        PayloadDigest.sha256(json));
        assertInvalid(plan(List.of(recursive.id()), List.of(recursive, reader)));
    }

    private static AgentExecutionPlan withDefinition(
            AgentExecutionPlan plan, ReleaseDefinition definition) {
        return new AgentExecutionPlan(
                plan.modelProfile(),
                plan.runtimeArtifactHash(),
                plan.requestedCapabilities(),
                plan.capabilities(),
                definition);
    }

    private static AgentExecutionPlan plan(
            List<String> requested, List<CapabilityBinding> bindings) {
        var base = base();
        var scopes =
                bindings.stream().flatMap(b -> b.scopes().stream()).distinct().sorted().toList();
        return new AgentExecutionPlan(
                "primary",
                ARTIFACT,
                requested,
                bindings,
                new ReleaseDefinition(
                        base.version(),
                        base.agent(),
                        scopes,
                        base.primary(),
                        base.fallback(),
                        base.maxModelCalls(),
                        base.maxToolCalls(),
                        base.business()));
    }

    private static CapabilityBinding binding(
            String id, String name, List<String> scopes, List<String> dependencies) {
        return new CapabilityBinding(
                id,
                "function",
                name,
                scopes,
                dependencies,
                CONTRACT,
                PayloadDigest.sha256(CONTRACT));
    }

    private static ReleaseDefinition base() {
        return new ReleaseDefinition(
                "base",
                "finance_agent",
                ReleaseDefinition.SCOPES,
                ReleaseDefinition.OFFLINE,
                null,
                12,
                32,
                new BusinessFeatures(List.of(), "skills", "{}", null));
    }

    private static void assertInvalid(AgentExecutionPlan plan) {
        assertThatThrownBy(() -> AgentPlanValidator.validate(plan, base(), "tenant"))
                .hasMessage("agent_plan_invalid");
    }
}
