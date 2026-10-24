package com.financeclaw.infrastructure.agentscope.capability;

import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.agent.*;
import com.financeclaw.domain.release.ReleaseDefinition;
import com.financeclaw.infrastructure.agentscope.RuntimeArtifacts;
import com.financeclaw.infrastructure.agentscope.skills.PackagedSkills;
import com.financeclaw.infrastructure.ziwei.ZiweiSchemas;
import com.financeclaw.support.*;
import com.financeclaw.ziwei.ZiweiDates;
import java.util.*;

/** Publish-time contracts for the actual local adapters and packaged Skill assets. */
public final class LocalCapabilityCatalog {
    public static final String ARTIFACT_READER = "function:read_artifact_range";

    private LocalCapabilityCatalog() {}

    public static List<CapabilityBinding> bindings(ReleaseDefinition base) {
        var result = new TreeMap<String, CapabilityBinding>();
        new LocalToolContribution(null, null, null)
                .contribute(
                        (name, description, schema, scope, action, interaction) -> {
                            var scopes = new TreeSet<String>();
                            if (scope != null) scopes.add(scope);
                            if (name.equals("search_catalog")) scopes.add("artifact:read");
                            add(
                                    result,
                                    base,
                                    function(
                                            name,
                                            description,
                                            schema,
                                            name.equals("request_city")
                                                    ? LocalToolContribution.CITY
                                                    : Map.of(),
                                            interaction != null && interaction.approval(),
                                            scopes.stream().toList(),
                                            dependencies(name)));
                        });
        add(
                result,
                base,
                function(
                        "market_snapshot",
                        MarketToolSchemas.SNAPSHOT,
                        Map.of(),
                        false,
                        List.of("market:read"),
                        dependencies("market_snapshot")));
        add(
                result,
                base,
                function(
                        "market_research",
                        MarketToolSchemas.RESEARCH,
                        MarketToolSchemas.ANSWER,
                        false,
                        List.of("market:read", "research:run"),
                        List.of(ARTIFACT_READER, "function:market_snapshot")));
        add(
                result,
                base,
                function(
                        "portfolio_review",
                        PortfolioInputs.SCHEMA,
                        Map.of(),
                        false,
                        List.of("market:read", "workflow:run"),
                        List.of(ARTIFACT_READER, "function:market_snapshot")));
        add(
                result,
                base,
                function(
                        "publish_portfolio_report",
                        PortfolioInputs.PUBLISH,
                        Map.of(),
                        true,
                        List.of("artifact:read", "report:write", "workflow:run"),
                        List.of(ARTIFACT_READER, "function:portfolio_review")));
        if (base.business().ziwei() != null) {
            for (var level : ZiweiDates.Level.values()) {
                String name = level.toolName();
                add(
                        result,
                        base,
                        function(
                                name,
                                ZiweiSchemas.description(name),
                                ZiweiSchemas.schema(name),
                                ZiweiSchemas.answerSchema(name),
                                false,
                                List.of("ziwei:read"),
                                dependencies(name)));
            }
        }
        if (base.scopes().contains("skill:use")) {
            for (var binding : PackagedSkills.bindings()) add(result, base, binding);
        }
        // A capability whose required dependency is unavailable must not appear selectable.
        boolean removed;
        do {
            removed =
                    result.values()
                            .removeIf(
                                    binding ->
                                            !result.keySet().containsAll(binding.dependencies()));
        } while (removed);
        return List.copyOf(result.values());
    }

    private static void add(
            Map<String, CapabilityBinding> result,
            ReleaseDefinition base,
            CapabilityBinding binding) {
        if (base.scopes().containsAll(binding.scopes())) result.put(binding.id(), binding);
    }

    private static List<String> dependencies(String name) {
        return name.equals("read_artifact_range") ? List.of() : List.of(ARTIFACT_READER);
    }

    private static CapabilityBinding function(
            String name,
            Map<String, Object> input,
            Map<String, Object> answer,
            boolean approval,
            List<String> scopes,
            List<String> dependencies) {
        return function(name, "Governed " + name, input, answer, approval, scopes, dependencies);
    }

    private static CapabilityBinding function(
            String name,
            String description,
            Map<String, Object> input,
            Map<String, Object> answer,
            boolean approval,
            List<String> scopes,
            List<String> dependencies) {
        String contract = functionContract(description, input, answer, approval);
        return new CapabilityBinding(
                "function:" + name,
                "function",
                name,
                scopes,
                dependencies,
                contract,
                PayloadDigest.sha256(contract));
    }

    public static String functionContract(
            String description,
            Map<String, Object> input,
            Map<String, Object> answer,
            boolean approval) {
        return PayloadJson.canonical(
                Map.of(
                        "description",
                        description,
                        "inputSchema",
                        input,
                        "humanAnswerSchema",
                        answer,
                        "approval",
                        approval));
    }

    /** Must run before checkpoint restore, resumed human-input work, or any model request. */
    public static void requireCompatible(AgentExecutionPlan plan) {
        if (plan == null) return;
        if (!RuntimeArtifacts.hash().equals(plan.runtimeArtifactHash()))
            throw new Blocked("runtime_release_changed");
        var available = new HashMap<String, CapabilityBinding>();
        bindings(plan.definition()).forEach(binding -> available.put(binding.id(), binding));
        for (var binding : plan.capabilities()) {
            if (binding.kind().equals("agent")) {
                // 目标来自父发布的不可变契约，不是当前本地 Function 目录中的动态别名。
                PrivateAgentContracts.from(binding);
            } else if (!binding.kind().equals("mcp")
                    && !binding.equals(available.get(binding.id()))) {
                throw new Blocked("capability_release_changed");
            }
        }
    }
}
