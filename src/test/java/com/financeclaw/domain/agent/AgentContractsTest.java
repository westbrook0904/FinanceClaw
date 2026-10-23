package com.financeclaw.domain.agent;

import static org.assertj.core.api.Assertions.*;

import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.Task.Problem;
import com.financeclaw.domain.release.BusinessFeatures;
import com.financeclaw.domain.release.ReleaseDefinition;
import java.util.List;
import java.util.UUID;
import org.junit.jupiter.api.Test;

class AgentContractsTest {
    @Test
    void promptBudgetCountsUtf8BytesAndPreservesOriginalContent() {
        assertThat(AgentDraft.validContent("Agent", "a".repeat(16_384))).isTrue();
        assertThat(AgentDraft.validContent("Agent", "中".repeat(5_461))).isTrue();
        assertThat(AgentDraft.validContent("Agent", "中".repeat(5_462))).isFalse();
        assertThat(AgentDraft.validContent("n".repeat(201), "prompt")).isFalse();
        assertThat(AgentDraft.validContent("Agent", "\n\t ")).isFalse();
        var draft = new AgentDraft(UUID.randomUUID().toString(), 1, " Agent ", " prompt\n", "base");
        assertThat(draft.displayName()).isEqualTo(" Agent ");
        assertThat(draft.systemPrompt()).isEqualTo(" prompt\n");
    }

    @Test
    void customIdentityRequiresCanonicalUuidAndBuiltinCannotBeEdited() {
        assertThat(AgentDraft.isCustomId(UUID.randomUUID().toString())).isTrue();
        assertThat(AgentDraft.isCustomId("1-1-1-1-1")).isFalse();
        assertThat(AgentDraft.isCustomId(null)).isFalse();
        assertThatThrownBy(() -> AgentDraft.requireCustomId("finance_agent"))
                .isInstanceOf(Problem.class)
                .hasMessage("agent_read_only");
        assertThatThrownBy(() -> AgentDraft.requireCustomId("not-an-agent"))
                .hasMessage("invalid_agent_id");
    }

    @Test
    void contentMustRoundTripThroughUtf8AndPostgresTextWithoutReplacement() {
        assertThat(AgentDraft.validContent("Agent", "😀".repeat(4_096))).isTrue();
        assertThat(AgentDraft.validContent("Agent", "😀".repeat(4_097))).isFalse();
        for (String invalid : List.of("bad\uD800", "bad\uDC00", "bad\0text")) {
            assertThat(AgentDraft.validContent(invalid, "prompt")).isFalse();
            assertThat(AgentDraft.validContent("Agent", invalid)).isFalse();
            assertThatThrownBy(() -> AgentDraft.requireContent("Agent", invalid))
                    .hasMessage("invalid_agent_draft");
        }
    }

    @Test
    void resolvedPrivateReleaseRequiresBothAgentAndOwnerWhileLegacyRemainsShared() {
        var owner = new Owner("tenant", "owner");
        String id = UUID.randomUUID().toString();
        var release = new ResolvedAgentRelease(id, "prompt", definition(), "tenant", "owner");
        release.requireIdentity(owner, id);
        assertThatThrownBy(() -> release.requireIdentity(new Owner("tenant", "other"), id))
                .hasMessage("agent_identity_mismatch");
        assertThatThrownBy(() -> release.requireIdentity(new Owner("other", "owner"), id))
                .hasMessage("agent_identity_mismatch");
        assertThatThrownBy(() -> release.requireIdentity(owner, UUID.randomUUID().toString()))
                .hasMessage("agent_identity_mismatch");
        new ResolvedAgentRelease(
                        "finance_agent", AgentDraft.BUILTIN_PROMPT, definition(), null, null)
                .requireIdentity(owner, "finance_agent");
    }

    @Test
    void manifestRejectsUnknownRuntimeAndFormat() {
        String id = UUID.randomUUID().toString();
        var valid =
                new AgentReleaseManifest(
                        AgentReleaseManifest.FORMAT,
                        id,
                        "tenant",
                        "owner",
                        1,
                        "Agent",
                        "prompt",
                        "a".repeat(64),
                        AgentReleaseManifest.RUNTIME_IDENTITY);
        valid.validate();
        assertThatThrownBy(
                        () ->
                                new AgentReleaseManifest(
                                                valid.format(),
                                                id,
                                                valid.tenant(),
                                                valid.subject(),
                                                1,
                                                valid.displayName(),
                                                valid.systemPrompt(),
                                                valid.baseReleaseHash(),
                                                "future-runtime")
                                        .validate())
                .hasMessage("agent_runtime_incompatible");
        assertThatThrownBy(
                        () ->
                                new AgentReleaseManifest(
                                                "unknown-format",
                                                id,
                                                valid.tenant(),
                                                valid.subject(),
                                                1,
                                                valid.displayName(),
                                                valid.systemPrompt(),
                                                valid.baseReleaseHash(),
                                                valid.runtimeIdentity())
                                        .validate())
                .hasMessage("agent_manifest_invalid");
    }

    private static ReleaseDefinition definition() {
        return new ReleaseDefinition(
                "v1",
                "finance_agent",
                List.of(),
                ReleaseDefinition.OFFLINE,
                null,
                12,
                32,
                new BusinessFeatures(List.of(), "skills", "{}", null));
    }
}
