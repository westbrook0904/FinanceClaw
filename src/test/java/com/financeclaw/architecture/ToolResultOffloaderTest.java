package com.financeclaw.architecture;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.Mockito.*;

import com.financeclaw.app.artifact.ArtifactService;
import com.financeclaw.domain.Governance.Artifact;
import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.Task.*;
import com.financeclaw.infrastructure.agentscope.context.ToolResultOffloader;
import com.financeclaw.support.ArtifactPayload;
import com.financeclaw.support.PayloadJson;
import io.agentscope.core.message.*;
import io.agentscope.core.util.JsonUtils;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.EnumSource;
import org.mockito.ArgumentCaptor;

class ToolResultOffloaderTest {
    private static final Claim CLAIM =
            new Claim(
                    new Command("command", "turn", "running", "attempt", 1, true, null),
                    new Turn(
                            "turn",
                            "conversation",
                            "tenant",
                            "subject",
                            "key",
                            "hash",
                            "input",
                            "release",
                            "running",
                            1,
                            null,
                            null));
    private ArtifactService artifacts;
    private ToolResultOffloader offloader;

    @BeforeEach
    void setup() {
        artifacts = mock(ArtifactService.class);
        offloader = new ToolResultOffloader(artifacts, 8192);
        when(artifacts.archiveToolResult(eq(CLAIM), any(), nullable(String.class)))
                .thenAnswer(
                        invocation -> {
                            var encoded = ArtifactPayload.encode(invocation.getArgument(1));
                            return new Artifact(
                                    "artifact",
                                    "turn",
                                    "conversation",
                                    "tenant",
                                    "subject",
                                    "release",
                                    encoded.hash(),
                                    encoded.json(),
                                    encoded.bytes());
                        });
    }

    @Test
    void smallResultKeepsItsIdentityAndDoesNotTouchTheDatabase() {
        var result = result("short", ToolResultState.SUCCESS, Map.of("source", "fixture"));
        assertThat(offloader.offload(CLAIM, result, "catalog:read")).isSameAs(result);
        verifyNoInteractions(artifacts);
    }

    @Test
    void thresholdMeasuresUtf8RatherThanCharacterCount() {
        var result = result("海".repeat(2800), ToolResultState.SUCCESS, Map.of());
        assertThat(JsonUtils.getJsonCodec().toJson(result).length()).isLessThan(8192);
        assertThat(offloader.offload(CLAIM, result, "catalog:read")).isNotSameAs(result);
        verify(artifacts).archiveToolResult(eq(CLAIM), any(), eq("catalog:read"));
    }

    @ParameterizedTest
    @EnumSource(
            value = ToolResultState.class,
            names = {"SUCCESS", "ERROR", "DENIED", "INTERRUPTED"})
    void completeResultsKeepCallAndStateWhileTheirEntireContentIsRecoverableFromTheFile(
            ToolResultState state) {
        var result =
                result(
                        "private-body-".repeat(2000),
                        state,
                        Map.of("source", "mcp", "diagnostic", "original-metadata"));
        var next = offloader.offload(CLAIM, result, "mcp:read");
        assertThat(next.getId()).isEqualTo(result.getId());
        assertThat(next.getName()).isEqualTo(result.getName());
        assertThat(next.getState()).isEqualTo(state);
        var text = ((TextBlock) next.getOutput().getFirst()).getText();
        assertThat(text).doesNotContain("private-body-", "original-metadata");
        var reference = PayloadJson.object(text);
        assertThat(reference)
                .containsEntry("artifact_id", "artifact")
                .containsEntry("archived", true)
                .containsEntry("download_url", "/v1/artifacts/artifact/download");
        assertThat(PayloadJson.canonical(reference.get("read_range")))
                .contains("\"tool\":\"read_artifact_range\"");
        assertThat(JsonUtils.getJsonCodec().toJson(next).getBytes(StandardCharsets.UTF_8).length)
                .isLessThan(ToolResultOffloader.MIN_THRESHOLD_BYTES);
        var captured = ArgumentCaptor.forClass(Object.class);
        verify(artifacts).archiveToolResult(eq(CLAIM), captured.capture(), eq("mcp:read"));
        assertThat(PayloadJson.canonical(captured.getValue()))
                .isEqualTo(
                        PayloadJson.canonical(
                                PayloadJson.parse(JsonUtils.getJsonCodec().toJson(result))));
        assertThat(result.getMetadata()).containsEntry("diagnostic", "original-metadata");
    }

    @Test
    void oversizedMetadataCannotBypassTheLimitOrRemainInTheReplacement() {
        var result =
                result("short", ToolResultState.SUCCESS, Map.of("large", "sensitive".repeat(2000)));
        var next = offloader.offload(CLAIM, result, "catalog:read");
        assertThat(next.getMetadata()).containsOnlyKeys("financeclaw_artifact");
        assertThat(JsonUtils.getJsonCodec().toJson(next)).doesNotContain("sensitive");
        verify(artifacts).archiveToolResult(eq(CLAIM), any(), eq("catalog:read"));
    }

    @Test
    void pausedAndInProgressResultsRemainUnderNativeRecoveryControl() {
        var suspended =
                ToolResultBlock.suspended(
                        ToolUseBlock.builder()
                                .id("call")
                                .name("request_city")
                                .input(Map.of())
                                .build());
        var running = result("large".repeat(3000), ToolResultState.RUNNING, Map.of());
        assertThat(offloader.offload(CLAIM, suspended, null)).isSameAs(suspended);
        assertThat(offloader.offload(CLAIM, running, null)).isSameAs(running);
        verifyNoInteractions(artifacts);
    }

    @Test
    void anExistingReferenceDoesNotGenerateAnotherArtifact() {
        var next =
                offloader.offload(
                        CLAIM,
                        result("body".repeat(4000), ToolResultState.SUCCESS, Map.of()),
                        "catalog:read");
        clearInvocations(artifacts);
        assertThat(offloader.offload(CLAIM, next, "catalog:read")).isSameAs(next);
        verifyNoInteractions(artifacts);
    }

    @Test
    void archivingFailurePropagatesWithoutReplacingOrTruncatingTheOriginal() {
        var failure = new Blocked("artifact_archive_failed");
        when(artifacts.archiveToolResult(eq(CLAIM), any(), eq("catalog:read"))).thenThrow(failure);
        var result = result("body".repeat(4000), ToolResultState.SUCCESS, Map.of());
        assertThatThrownBy(() -> offloader.offload(CLAIM, result, "catalog:read"))
                .isSameAs(failure);
        assertThat(((TextBlock) result.getOutput().getFirst()).getText()).hasSize(16000);
    }

    @Test
    void payloadBeyondTheStoredLimitFailsExplicitlyBeforePublishingAnyReference() {
        var result = result("x".repeat(2097152), ToolResultState.SUCCESS, Map.of());
        assertThatThrownBy(() -> offloader.offload(CLAIM, result, "catalog:read"))
                .isInstanceOf(Blocked.class)
                .hasMessage("artifact_too_large");
        verifyNoInteractions(artifacts);
    }

    @Test
    void restoredContextReplacesEveryCompleteResultIncludingSkillsWithoutMutatingMessages() {
        var user = Msg.builder().role(MsgRole.USER).textContent("input").build();
        var large = result("skill-data".repeat(2000), ToolResultState.SUCCESS, Map.of());
        var message =
                Msg.builder()
                        .id("message")
                        .name("tools")
                        .role(MsgRole.TOOL)
                        .content(large)
                        .metadata(Map.of("preserve", "message-metadata"))
                        .build();
        var original = List.of(user, message);
        var next = offloader.offloadMessages(CLAIM, original, name -> "skill:use");
        assertThat(next.getFirst()).isSameAs(user);
        assertThat(next.get(1)).isNotSameAs(message);
        assertThat(next.get(1).getId()).isEqualTo(message.getId());
        assertThat(next.get(1).getMetadata()).isEqualTo(message.getMetadata());
        assertThat(message.getFirstContentBlock()).isSameAs(large);
        verify(artifacts).archiveToolResult(eq(CLAIM), any(), eq("skill:use"));
    }

    @Test
    void historicalReferencesAreArchivedAsAnOwnerScopedIndexWithoutCopyingTheirBodies() {
        var references =
                List.of(
                        "artifact_ref:{\"artifact_id\":\"old-index\",\"bytes\":500,\"hash\":\"old-hash\"}",
                        "artifact_ref:{\"artifact_id\":\"original\",\"bytes\":20000,\"hash\":\"original-hash\"}");
        var result = offloader.archiveReferences(CLAIM, references, "skill:use");
        var payload = ArgumentCaptor.forClass(Object.class);
        verify(artifacts).archiveToolResult(eq(CLAIM), payload.capture(), eq("skill:use"));
        assertThat(payload.getValue()).isEqualTo(Map.of("artifact_refs", references));
        assertThat(result).hasSize(1);
        assertThat(result.getFirst())
                .startsWith("artifact_ref:")
                .doesNotContain("old-index", "original", "old-hash");
        var reference = PayloadJson.object(result.getFirst().substring("artifact_ref:".length()));
        assertThat(reference)
                .containsEntry("artifact_id", "artifact")
                .containsEntry("hash", ArtifactPayload.encode(payload.getValue()).hash())
                .containsEntry("bytes", ArtifactPayload.encode(payload.getValue()).bytes());
        verify(artifacts, never()).readRange(any(), any(), anyInt(), anyInt());
        verify(artifacts, never()).download(any(), any());
    }

    @Test
    void indexFailureCannotDiscardTheOriginalReferences() {
        var references = List.of("artifact_ref:{\"artifact_id\":\"original\"}");
        var failure = new Blocked("artifact_archive_failed");
        when(artifacts.archiveToolResult(eq(CLAIM), any(), eq("skill:use"))).thenThrow(failure);
        assertThatThrownBy(() -> offloader.archiveReferences(CLAIM, references, "skill:use"))
                .isSameAs(failure);
        assertThat(references).containsExactly("artifact_ref:{\"artifact_id\":\"original\"}");
    }

    private static ToolResultBlock result(
            String body, ToolResultState state, Map<String, Object> metadata) {
        return new ToolResultBlock(
                "call", "tool", List.of(TextBlock.builder().text(body).build()), metadata, state);
    }
}
