package com.financeclaw.infrastructure.agentscope.capability;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.Mockito.*;

import com.financeclaw.app.agent.port.CredentialAccessGateway;
import com.financeclaw.app.artifact.ArtifactService;
import com.financeclaw.app.execution.ChildInvocationService;
import com.financeclaw.app.execution.ExecutionPolicyService;
import com.financeclaw.app.market.MarketDataService;
import com.financeclaw.app.report.ReportService;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;
import com.financeclaw.domain.agent.*;
import com.financeclaw.domain.release.*;
import com.financeclaw.infrastructure.agentscope.RuntimeArtifacts;
import com.financeclaw.support.PayloadJson;
import io.agentscope.core.message.*;
import io.agentscope.core.tool.*;
import java.util.*;
import java.util.concurrent.atomic.*;
import java.util.function.Supplier;
import org.junit.jupiter.api.Test;

/**
 * Native MCP transport/tool registration, with the business transaction authority kept explicit.
 */
class ReadOnlyLeafToolsTest {
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
                            "a".repeat(64),
                            "running",
                            1,
                            null,
                            null));

    @Test
    void frozenReadOnlyMcpUsesNativeTransportAndPersistsOnlyInsideChildBoundary() throws Exception {
        try (var remote = new McpContractFixture();
                var unused = new McpContractFixture()) {
            var fixture = new Fixture(remote, unused);
            try (var session = fixture.open()) {
                assertThat(session.toolkit.getToolNames())
                        .containsExactlyInAnyOrder("search", "read_artifact_range");
                assertThat(session.toolkit.getTool("search")).isInstanceOf(ToolBase.class);
                int discovered = remote.lists.get();
                var result = session.toolkit.getTool("search").callAsync(param()).block();
                assertThat(result).isNotNull();
                assertThat(result.getState()).isEqualTo(ToolResultState.SUCCESS);
                assertThat(PayloadJson.canonical(result)).contains("artifact_id");
                assertThat(remote.calls.get()).isOne();
                assertThat(remote.lists.get()).isEqualTo(discovered + 1);
                assertThat(unused.lists.get()).isZero();
                verify(fixture.access, never()).requireCredential(any(), eq("UNUSED_TOKEN"));
                verify(fixture.calls)
                        .reservePrivateRead(
                                eq(fixture.invocation), eq("read"), eq("search"), anyMap());
                verify(fixture.calls)
                        .completePrivateRead(
                                eq(fixture.invocation), eq("read"), eq("search"), anyMap(), any());
                verify(fixture.artifacts).archiveToolResult(eq(CLAIM), any(), eq("travel:read"));
            }
        }
    }

    @Test
    void credentialRevocationAfterAssemblyPreventsNativeCall() throws Exception {
        try (var remote = new McpContractFixture();
                var unused = new McpContractFixture()) {
            var fixture = new Fixture(remote, unused);
            try (var session = fixture.open()) {
                doThrow(new Blocked("credential_access_denied"))
                        .when(fixture.access)
                        .requireCredential(new Owner("tenant", "subject"), "SELECTED_TOKEN");
                assertThatThrownBy(
                                () -> session.toolkit.getTool("search").callAsync(param()).block())
                        .isInstanceOf(Blocked.class)
                        .hasMessage("credential_access_denied");
                assertThat(remote.calls.get()).isZero();
                verifyNoInteractions(fixture.artifacts);
            }
        }
    }

    @Test
    void targetRevocationDuringRemoteCallPreventsArtifactAndCompletionReceipt() throws Exception {
        try (var remote = new McpContractFixture();
                var unused = new McpContractFixture()) {
            var fixture = new Fixture(remote, unused);
            fixture.revokeAfterRemote.set(true);
            try (var session = fixture.open()) {
                assertThatThrownBy(
                                () -> session.toolkit.getTool("search").callAsync(param()).block())
                        .isInstanceOf(Blocked.class)
                        .hasMessage("agent_access_denied");
                assertThat(remote.calls.get()).isOne();
                verifyNoInteractions(fixture.artifacts);
                verify(fixture.calls, never())
                        .completePrivateRead(any(), anyString(), anyString(), anyMap(), any());
            }
        }
    }

    private static final class Fixture {
        final ChildInvocationService calls = mock(ChildInvocationService.class);
        final ArtifactService artifacts = mock(ArtifactService.class);
        final CredentialAccessGateway access = mock(CredentialAccessGateway.class);
        final AtomicBoolean revokeAfterRemote = new AtomicBoolean();
        final AtomicInteger boundary = new AtomicInteger();
        final PrivateAgentTarget target;
        final ResolvedAgentRelease release;
        final ChildInvocationService.Invocation invocation;
        final ReadOnlyLeafTools factory;

        Fixture(McpContractFixture remote, McpContractFixture unused) {
            var base =
                    McpContractFixture.release(
                            remote.remote("hotel", "SELECTED_TOKEN"),
                            unused.remote("taibu", "UNUSED_TOKEN"));
            var credentials = McpCapabilityDiscoveryTest.credentials();
            var bindings =
                    new ArrayList<>(
                            new McpCapabilityDiscovery(credentials)
                                    .discover(
                                            new Owner("tenant", "subject"),
                                            base,
                                            List.of("mcp:hotel/search")));
            bindings.add(
                    LocalCapabilityCatalog.bindings(base).stream()
                            .filter(binding -> binding.id().equals("function:read_artifact_range"))
                            .findFirst()
                            .orElseThrow());
            bindings.sort(Comparator.comparing(CapabilityBinding::id));
            var definition =
                    new ReleaseDefinition(
                            base.version(),
                            base.agent(),
                            List.of("travel:read"),
                            base.primary(),
                            null,
                            10,
                            10,
                            base.business());
            var plan =
                    new AgentExecutionPlan(
                            "primary",
                            RuntimeArtifacts.hash(),
                            List.of("mcp:hotel/search"),
                            bindings,
                            definition);
            target =
                    new PrivateAgentTarget(
                            UUID.randomUUID().toString(),
                            "b".repeat(64),
                            PrivateAgentTarget.Profile.READ_ONLY,
                            List.of("travel:read"));
            release =
                    new ResolvedAgentRelease(
                            target.agentId(),
                            "selected only",
                            definition,
                            "tenant",
                            "subject",
                            plan);
            invocation =
                    new ChildInvocationService.Invocation(
                            CLAIM,
                            "delegate",
                            "c".repeat(64),
                            null,
                            List.of(),
                            "private_agent",
                            target.agentId(),
                            target.releaseHash(),
                            4,
                            10,
                            target.profile(),
                            target.scopes());
            doAnswer(
                            call -> {
                                if (revokeAfterRemote.get() && remote.calls.get() > 0)
                                    throw new Blocked("agent_access_denied");
                                boundary.incrementAndGet();
                                try {
                                    return ((Supplier<?>) call.getArgument(1)).get();
                                } finally {
                                    boundary.decrementAndGet();
                                }
                            })
                    .when(calls)
                    .checkpoint(eq(invocation), any());
            when(calls.completePrivateRead(
                            eq(invocation), anyString(), anyString(), anyMap(), any()))
                    .thenAnswer(call -> call.getArgument(4));
            when(artifacts.archiveToolResult(eq(CLAIM), any(), eq("travel:read")))
                    .thenAnswer(
                            call -> {
                                assertThat(boundary.get()).isPositive();
                                return new Artifact(
                                        "artifact",
                                        "turn",
                                        "conversation",
                                        "tenant",
                                        "subject",
                                        "release",
                                        "d".repeat(64),
                                        "{}",
                                        2);
                            });
            factory =
                    new ReadOnlyLeafTools(
                            calls,
                            mock(ExecutionPolicyService.class),
                            credentials,
                            artifacts,
                            mock(MarketDataService.class),
                            mock(ReportService.class),
                            mock(ChildInteractionBridge.class));
            factory.configureCredentialAccess(access);
        }

        ReadOnlyLeafTools.Session open() {
            return factory.open(target, invocation, release, new AtomicReference<>());
        }
    }

    private static ToolCallParam param() {
        var input = Map.<String, Object>of("city", "Paris");
        return ToolCallParam.builder()
                .toolUseBlock(ToolUseBlock.builder().id("read").name("search").input(input).build())
                .input(input)
                .build();
    }
}
