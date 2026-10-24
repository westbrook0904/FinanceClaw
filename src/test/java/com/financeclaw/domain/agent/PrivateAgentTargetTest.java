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
