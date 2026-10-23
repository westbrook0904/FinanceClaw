package com.financeclaw.architecture;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.Mockito.*;

import com.financeclaw.app.artifact.ArtifactService;
import com.financeclaw.app.execution.ExecutionPolicyService;
import com.financeclaw.app.release.port.Credentials;
import com.financeclaw.domain.Governance.Artifact;
import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.Task.*;
import com.financeclaw.domain.release.*;
import com.financeclaw.infrastructure.agentscope.capability.McpToolContribution;
import com.financeclaw.support.ArtifactPayload;
import com.financeclaw.support.PayloadJson;
import com.financeclaw.tools.McpTools;
import io.agentscope.core.message.*;
import io.agentscope.core.tool.*;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.function.Function;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;
import reactor.core.publisher.Mono;

/** 只替换 MCP 连接构造，验证实际贡献器保留原工具权限、隐私归档和可读取引用。 */
class McpResultArchiveTest {
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

    @Test
    void mcpOnlyScopeArchivesWithPrivacyAndReturnsRangeAndDownloadInstructions() {
        var artifacts = mock(ArtifactService.class);
        var policy = mock(ExecutionPolicyService.class);
        var body = Map.of("hotels", List.of(Map.of("description", "tool-body".repeat(3000))));
        when(artifacts.archiveToolResult(eq(CLAIM), any(), eq("travel:read")))
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
        var toolkit = toolkit(ToolResultBlock.text(PayloadJson.canonical(body)));
        var actions = new HashMap<String, Function<ToolCallParam, Mono<ToolResultBlock>>>();
        var scopes = new HashMap<String, String>();
        try (var transport = mockConstruction(McpTools.class);
                var contribution =
                        new McpToolContribution(
                                policy,
                                mock(Credentials.class),
                                artifacts,
                                CLAIM,
                                release(),
                                List.of("travel:read"),
                                toolkit)) {
            contribution.contribute(
                    (name, description, schema, scope, action, interaction) -> {
                        actions.put(name, action);
                        scopes.put(name, scope);
                    });
            var result =
                    actions.get("hotel_search")
                            .apply(
                                    ToolCallParam.builder()
                                            .toolUseBlock(call())
                                            .input(Map.of())
                                            .build())
                            .block();
            var reference =
                    PayloadJson.object(((TextBlock) result.getOutput().getFirst()).getText());
            assertThat(scopes).containsEntry("hotel_search", "travel:read");
            assertThat(reference)
                    .containsEntry("artifact_id", "artifact")
                    .containsEntry("download_url", "/v1/artifacts/artifact/download");
            assertThat(PayloadJson.canonical(reference.get("read_range")))
                    .contains("read_artifact_range", "artifact", "nextStart");
            var payload = ArgumentCaptor.forClass(Object.class);
            verify(artifacts).archiveToolResult(eq(CLAIM), payload.capture(), eq("travel:read"));
            assertThat(payload.getValue()).isEqualTo(body);
            verify(artifacts, never()).archive(any(), any(), any());
            verify(policy).checkDispatch(CLAIM);
            assertThat(transport.constructed()).hasSize(1);
        }
    }

    @Test
    void failedArchivingCannotPublishAMcpReference() {
        var artifacts = mock(ArtifactService.class);
        var failure = new Blocked("artifact_archive_failed");
        when(artifacts.archiveToolResult(eq(CLAIM), any(), eq("travel:read"))).thenThrow(failure);
        var actions = new HashMap<String, Function<ToolCallParam, Mono<ToolResultBlock>>>();
        try (var transport = mockConstruction(McpTools.class);
                var contribution =
                        new McpToolContribution(
                                mock(ExecutionPolicyService.class),
                                mock(Credentials.class),
                                artifacts,
                                CLAIM,
                                release(),
                                List.of("travel:read"),
                                toolkit(ToolResultBlock.text("{\"hotels\":[]}")))) {
            contribution.contribute(
                    (name, description, schema, scope, action, interaction) ->
                            actions.put(name, action));
            assertThatThrownBy(
                            () ->
                                    actions.get("hotel_search")
                                            .apply(
                                                    ToolCallParam.builder()
                                                            .toolUseBlock(call())
                                                            .input(Map.of())
                                                            .build())
                                            .block())
                    .isSameAs(failure);
            assertThat(transport.constructed()).hasSize(1);
        }
    }

    private static Toolkit toolkit(ToolResultBlock output) {
        var toolkit = new Toolkit();
        toolkit.registerAgentTool(
                new ToolBase(
                        ToolBase.builder()
                                .name("hotel_search")
                                .description("fixture native MCP tool")
                                .inputSchema(Map.of("type", "object", "properties", Map.of()))) {
                    @Override
                    public Mono<ToolResultBlock> callAsync(ToolCallParam param) {
                        return Mono.just(output);
                    }
                });
        return toolkit;
    }

    private static ReleaseDefinition release() {
        var remote =
                new RemoteEndpoint(
                        "travel",
                        "http://127.0.0.1:9000/mcp",
                        List.of("127.0.0.1:9000"),
                        "fixture-token",
                        false,
                        List.of(),
                        List.of("hotel_search"),
                        1000);
        return new ReleaseDefinition(
                "test",
                "finance_agent",
                List.of("travel:read"),
                ReleaseDefinition.OFFLINE,
                null,
                10,
                10,
                new BusinessFeatures(List.of(remote), "", "{}", null));
    }

    private static ToolUseBlock call() {
        return ToolUseBlock.builder()
                .id("call")
                .name("hotel_search")
                .input(Map.of())
                .content("{}")
                .build();
    }
}
