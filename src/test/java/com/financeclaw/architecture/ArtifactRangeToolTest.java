package com.financeclaw.architecture;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.Mockito.*;

import com.financeclaw.app.artifact.ArtifactService;
import com.financeclaw.domain.Governance.Artifact;
import com.financeclaw.domain.Task.*;
import com.financeclaw.infrastructure.agentscope.capability.LocalToolContribution;
import com.financeclaw.infrastructure.agentscope.context.ToolResultOffloader;
import com.financeclaw.support.ArtifactPayload;
import io.agentscope.core.message.*;
import io.agentscope.core.tool.ToolCallParam;
import io.agentscope.core.tool.ToolValidator;
import io.agentscope.core.util.JsonUtils;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.Map;
import java.util.function.Function;
import org.junit.jupiter.api.Test;
import reactor.core.publisher.Mono;

class ArtifactRangeToolTest {
    @Test
    void rangeToolDelegatesAuthorizationAndKeepsEscapedContentBelowTheOffloadThreshold() {
        var claim =
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
        var artifacts = mock(ArtifactService.class);
        var actions = new HashMap<String, Function<ToolCallParam, Mono<ToolResultBlock>>>();
        var schemas = new HashMap<String, Map<String, Object>>();
        var scopes = new HashMap<String, String>();
        new LocalToolContribution(artifacts, null, claim)
                .contribute(
                        (name, description, schema, scope, action, interaction) -> {
                            actions.put(name, action);
                            schemas.put(name, schema);
                            scopes.put(name, scope);
                        });
        var encoded = ArtifactPayload.encode(Map.of("output", "\r\n\t\b\f\\\"".repeat(1000)));
        var artifact =
                new Artifact(
                        "artifact",
                        "turn",
                        "conversation",
                        "tenant",
                        "subject",
                        "release",
                        encoded.hash(),
                        encoded.json(),
                        encoded.bytes());
        var owner = new Owner("tenant", "subject");
        when(artifacts.readRange(owner, "artifact", 0, 512))
                .thenReturn(ArtifactPayload.readRange(artifact, 0, 512));
        var result =
                actions.get("read_artifact_range")
                        .apply(
                                ToolCallParam.builder()
                                        .input(Map.of("artifact_id", "artifact"))
                                        .build())
                        .block();
        assertThat(scopes).containsKey("read_artifact_range");
        assertThat(scopes.get("read_artifact_range")).isNull();
        assertThat(schemas.get("read_artifact_range")).containsEntry("additionalProperties", false);
        var schema = schemas.get("read_artifact_range");
        assertThat(
                        ToolValidator.validateInput(
                                "{\"artifact_id\":\"artifact\",\"start\":2097152}", schema))
                .isNull();
        assertThat(
                        ToolValidator.validateInput(
                                "{\"artifact_id\":\"artifact\",\"start\":2097153}", schema))
                .isNotNull();
        assertThat(
                        ToolValidator.validateInput(
                                "{\"artifact_id\":\"artifact\",\"start\":4294967296}", schema))
                .isNotNull();
        assertThat(JsonUtils.getJsonCodec().toJson(result).getBytes(StandardCharsets.UTF_8).length)
                .isLessThan(ToolResultOffloader.MIN_THRESHOLD_BYTES);
        assertThat(
                        new ToolResultOffloader(artifacts, ToolResultOffloader.MIN_THRESHOLD_BYTES)
                                .offload(claim, result, null))
                .isSameAs(result);
        verify(artifacts).readRange(owner, "artifact", 0, 512);
        verify(artifacts, never()).archiveToolResult(any(), any(), any());
    }
}
