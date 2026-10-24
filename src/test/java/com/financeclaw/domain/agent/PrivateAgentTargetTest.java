package com.financeclaw.domain.agent;

import static org.assertj.core.api.Assertions.*;

import com.financeclaw.domain.ExecutionScope;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.release.BusinessFeatures;
import com.financeclaw.domain.release.ReleaseDefinition;
import com.financeclaw.support.PayloadDigest;
import com.financeclaw.support.PayloadJson;
import com.financeclaw.support.PrivateAgentContracts;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;

class PrivateAgentTargetTest {
    private static final String ID = "11e8e638-0689-40df-99ed-b22de07c1172";
    private static final String HASH = "b".repeat(64);
    private static final Owner OWNER = new Owner("租户", "用户😀");

    @Test
    void bindingFreezesOnlyExplicitMessageAndTheExactTargetWithParentArchiveSupport() {
        var target = new PrivateAgentTarget(ID, HASH);
        var binding = PrivateAgentContracts.binding(target);
        binding.validate();
        assertThat(PrivateAgentContracts.from(binding)).isEqualTo(target);
        assertThat(binding).isEqualTo(PrivateAgentContracts.binding(ID, HASH));
        assertThat(binding.name()).isEqualTo("delegate_11e8e638068940df99edb22de07c1172");
        assertThat(binding.scopes()).containsExactly("artifact:read");
        assertThat(binding.dependencies()).containsExactly("function:read_artifact_range");
        var contract = PayloadJson.object(binding.contractJson());
        assertThat(contract)
                .containsEntry("agentId", ID)
                .containsEntry("releaseHash", HASH)
                .containsEntry("inputSchema", PrivateAgentTarget.INPUT_SCHEMA)
                .containsEntry("approval", false)
                .containsEntry("maxDepth", 1)
                .containsEntry("maxModelCalls", 4)
                .containsEntry("maxToolCalls", 0);
        assertThat(contract).doesNotContainKeys("owner", "systemPrompt", "memory");
    }

    @Test
    void emptyProfilePreservesTheOriginalV1CanonicalContractByteForByte() {
        String original =
                "{\"agentId\":\""
                        + ID
                        + "\",\"approval\":false,\"description\":\"Delegate an explicit message to a release-pinned private leaf Agent.\","
                        + "\"format\":\"agent-leaf/1\",\"inputSchema\":{\"additionalProperties\":false,"
                        + "\"properties\":{\"message\":{\"maxLength\":16384,\"minLength\":1,\"type\":\"string\"}},"
                        + "\"required\":[\"message\"],\"type\":\"object\"},\"maxDepth\":1,\"maxModelCalls\":4,"
                        + "\"maxToolCalls\":0,\"releaseHash\":\""
                        + HASH
                        + "\"}";
        var target = new PrivateAgentTarget(ID, HASH);
        assertThat(target.profile()).isEqualTo(PrivateAgentTarget.Profile.EMPTY);
        assertThat(PrivateAgentContracts.binding(target).contractJson()).isEqualTo(original);
        assertThat(PrivateAgentContracts.binding(target).contentHash())
                .isEqualTo(PayloadDigest.sha256(original));
    }

    @Test
    void marketContractPinsTheClosedProfilePermissionsAndOneToolWithoutExposingTheReader() {
        var target = new PrivateAgentTarget(ID, HASH, PrivateAgentTarget.Profile.MARKET_SNAPSHOT);
        var binding = PrivateAgentContracts.binding(target);
        assertThat(PrivateAgentContracts.from(binding)).isEqualTo(target);
        assertThat(binding.scopes()).containsExactly("artifact:read", "market:read");
        assertThat(binding.dependencies()).containsExactly("function:read_artifact_range");
        assertThat(target.resultScope()).isEqualTo("market:read");
        var contract = PayloadJson.object(binding.contractJson());
        assertThat(contract)
                .containsEntry("format", "agent-leaf/2")
                .containsEntry("profile", "market-snapshot-only")
                .containsEntry("maxToolCalls", 1)
                .containsEntry("childTools", List.of("market_snapshot"))
                .containsEntry("toolScopes", List.of("market:read"))
                .containsEntry("resultScope", "market:read");
        for (var change :
                List.of(
                        Map.entry("profile", (Object) "arbitrary-tools"),
                        Map.entry(
                                "childTools",
                                (Object) List.of("market_snapshot", "read_artifact_range")),
                        Map.entry("resultScope", (Object) "artifact:read"),
                        Map.entry("maxToolCalls", (Object) 2))) {
            var changed = new LinkedHashMap<>(contract);
            changed.put(change.getKey(), change.getValue());
            String json = PayloadJson.canonical(changed);
            var forged =
                    new CapabilityBinding(
                            binding.id(),
                            binding.kind(),
                            binding.name(),
                            binding.scopes(),
                            binding.dependencies(),
                            json,
                            PayloadDigest.sha256(json));
            assertThatThrownBy(() -> PrivateAgentContracts.from(forged))
                    .hasMessage("agent_plan_invalid");
        }
    }

    @Test
    void marketLeafRequiresExactlyItsSnapshotRequestAndPublishedReaderClosure() {
        var market = definition(List.of("market:read"));
        var snapshot =
                new CapabilityBinding(
                        PrivateAgentTarget.SNAPSHOT,
                        "function",
                        "market_snapshot",
                        List.of("market:read"),
                        List.of(PrivateAgentTarget.READER),
                        "{}",
                        HASH);
        var reader =
                new CapabilityBinding(
                        PrivateAgentTarget.READER,
                        "function",
                        "read_artifact_range",
                        List.of(),
                        List.of(),
                        "{}",
                        HASH);
        var plan =
                new AgentExecutionPlan(
                        "primary",
                        HASH,
                        List.of(PrivateAgentTarget.SNAPSHOT),
                        List.of(snapshot, reader),
                        market);
        var leaf = resolved(OWNER, market, plan);
        PrivateAgentTarget.requireLeaf(OWNER, leaf, PrivateAgentTarget.Profile.MARKET_SNAPSHOT);
        assertThatThrownBy(() -> PrivateAgentTarget.requireLeaf(OWNER, leaf))
                .hasMessage("agent_leaf_required");
        for (var invalid :
                List.of(
                        new AgentExecutionPlan(
                                "primary", HASH, List.of(), List.of(snapshot, reader), market),
                        new AgentExecutionPlan(
                                "primary",
                                HASH,
                                List.of(PrivateAgentTarget.SNAPSHOT),
                                List.of(snapshot),
                                market),
                        new AgentExecutionPlan(
                                "primary",
                                HASH,
                                List.of(PrivateAgentTarget.SNAPSHOT, PrivateAgentTarget.READER),
                                List.of(snapshot, reader),
                                market),
                        new AgentExecutionPlan(
                                "primary",
                                HASH,
                                List.of(PrivateAgentTarget.SNAPSHOT),
                                List.of(reader, snapshot),
                                market))) {
            assertThatThrownBy(
                            () ->
                                    PrivateAgentTarget.requireLeaf(
                                            OWNER,
                                            resolved(OWNER, market, invalid),
                                            PrivateAgentTarget.Profile.MARKET_SNAPSHOT))
                    .hasMessage("agent_leaf_required");
        }
        var expanded = definition(List.of("artifact:read", "market:read"));
        var expandedPlan =
                new AgentExecutionPlan(
                        "primary",
                        HASH,
                        List.of(PrivateAgentTarget.SNAPSHOT),
                        List.of(snapshot, reader),
                        expanded);
        assertThatThrownBy(
                        () ->
                                PrivateAgentTarget.requireLeaf(
                                        OWNER,
                                        resolved(OWNER, expanded, expandedPlan),
                                        PrivateAgentTarget.Profile.MARKET_SNAPSHOT))
                .hasMessage("agent_leaf_required");
    }

    @Test
    void validJsonAndFreshDigestCannotAuthorizeDifferentBudgetsOrExtraContractFields() {
        var binding = PrivateAgentContracts.binding(ID, HASH);
        for (String changed : List.of("maxDepth", "maxToolCalls", "owner")) {
            var contract = new LinkedHashMap<>(PayloadJson.object(binding.contractJson()));
            contract.put(changed, changed.equals("owner") ? "someone-else" : 7);
            String json = PayloadJson.canonical(contract);
            var forged =
                    new CapabilityBinding(
                            binding.id(),
                            binding.kind(),
                            binding.name(),
                            binding.scopes(),
                            binding.dependencies(),
                            json,
                            PayloadDigest.sha256(json));
            assertThatThrownBy(() -> PrivateAgentContracts.from(forged))
                    .hasMessage("agent_plan_invalid");
        }
        var renamed =
                new CapabilityBinding(
                        binding.id(),
                        "agent",
                        "different_tool",
                        binding.scopes(),
                        binding.dependencies(),
                        binding.contractJson(),
                        binding.contentHash());
        assertThatThrownBy(() -> PrivateAgentContracts.from(renamed))
                .hasMessage("agent_plan_invalid");
        var noReader =
                new CapabilityBinding(
                        binding.id(),
                        "agent",
                        binding.name(),
                        List.of(),
                        List.of(),
                        binding.contractJson(),
                        binding.contentHash());
        assertThatThrownBy(() -> PrivateAgentContracts.from(noReader))
                .hasMessage("agent_plan_invalid");
        assertThat(AgentComposition.validId("agent:" + ID)).isTrue();
        for (String invalid :
                List.of("agent:finance_agent", "agent:1-1-1-1-1", "agent:" + ID.toUpperCase())) {
            assertThat(AgentComposition.validId(invalid)).isFalse();
        }
    }

    @Test
    void leafRequiresBothOwnerAndExplicitlyEmptyRequestedActualAndScopeSets() {
        var empty = definition(List.of());
        var plan = new AgentExecutionPlan("primary", HASH, List.of(), List.of(), empty);
        var valid = resolved(OWNER, empty, plan);
        PrivateAgentTarget.requireLeaf(OWNER, valid);
        assertThatThrownBy(
                        () ->
                                PrivateAgentTarget.requireLeaf(
                                        new Owner("other", OWNER.subject()), valid))
                .hasMessage("agent_identity_mismatch");
        assertThatThrownBy(
                        () -> PrivateAgentTarget.requireLeaf(OWNER, resolved(OWNER, empty, null)))
                .hasMessage("agent_leaf_required");
        var requested =
                new AgentExecutionPlan(
                        "primary", HASH, List.of("function:read_artifact_range"), List.of(), empty);
        assertThatThrownBy(
                        () ->
                                PrivateAgentTarget.requireLeaf(
                                        OWNER, resolved(OWNER, empty, requested)))
                .hasMessage("agent_leaf_required");
        var actual =
                new AgentExecutionPlan(
                        "primary",
                        HASH,
                        List.of(),
                        List.of(PrivateAgentContracts.binding(ID, HASH)),
                        empty);
        assertThatThrownBy(
                        () -> PrivateAgentTarget.requireLeaf(OWNER, resolved(OWNER, empty, actual)))
                .hasMessage("agent_leaf_required");
        var scopes = definition(List.of("artifact:read"));
        var scopedPlan = new AgentExecutionPlan("primary", HASH, List.of(), List.of(), scopes);
        assertThatThrownBy(
                        () ->
                                PrivateAgentTarget.requireLeaf(
                                        OWNER, resolved(OWNER, scopes, scopedPlan)))
                .hasMessage("agent_leaf_required");
    }

    @Test
    void scopeIncludesTheParentAttemptCallTargetAndImmutableRelease() {
        var root =
                new ExecutionScope(
                        OWNER.tenant(),
                        OWNER.subject(),
                        "parent-attempt",
                        "parent",
                        "a".repeat(64));
        var target = new PrivateAgentTarget(ID, HASH);
        var scope = target.scope(root, "调用:😀");
        assertThat(scope)
                .isEqualTo(
                        new ExecutionScope(
                                OWNER.tenant(),
                                OWNER.subject(),
                                ExecutionScope.hash(
                                        List.of(root.key(), "private_agent", "调用:😀", ID, HASH)),
                                ID,
                                HASH));
        assertThat(scope.key()).isNotEqualTo(target.scope(root, "another-call").key());
        assertThat(scope.key())
                .isNotEqualTo(
                        new PrivateAgentTarget(ID, "c".repeat(64)).scope(root, "调用:😀").key());
    }

    private static ResolvedAgentRelease resolved(
            Owner owner, ReleaseDefinition definition, AgentExecutionPlan plan) {
        return new ResolvedAgentRelease(
                ID, "leaf", definition, owner.tenant(), owner.subject(), plan);
    }

    private static ReleaseDefinition definition(List<String> scopes) {
        return new ReleaseDefinition(
                "v1",
                "finance_agent",
                scopes,
                ReleaseDefinition.OFFLINE,
                null,
                12,
                32,
                new BusinessFeatures(List.of(), "skills", "{}", null));
    }
}
