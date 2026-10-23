package com.financeclaw.infrastructure.agentscope.capability;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.Mockito.*;

import com.financeclaw.app.artifact.ArtifactService;
import com.financeclaw.app.execution.ExecutionPolicyService;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;
import com.financeclaw.domain.agent.*;
import com.financeclaw.domain.release.*;
import io.agentscope.core.message.*;
import io.agentscope.core.tool.*;
import java.util.*;
import java.util.function.Function;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import reactor.core.publisher.Mono;

class McpFrozenContributionTest {
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
    void registersOnlySelectedWrappersAndRechecksBeforeNativeEffect() throws Exception {
        try (var remote = new McpContractFixture();
                var unselected = new McpContractFixture()) {
            var release =
                    McpContractFixture.release(
                            remote.remote("hotel", ""), unselected.remote("taibu", "unused-token"));
            var credentials = McpCapabilityDiscoveryTest.credentials();
            var plan = freeze(release);
            var policy = mock(ExecutionPolicyService.class);
            var artifacts = artifacts();
            var main = new Toolkit();
            var actions = new HashMap<String, Function<ToolCallParam, Mono<ToolResultBlock>>>();
            try (var contribution =
                    new McpToolContribution(
                            policy,
                            credentials,
                            artifacts,
                            CLAIM,
                            release,
                            List.of("travel:read"),
                            main,
                            plan)) {
                contribution.contribute(
                        (name, description, schema, scope, action, interaction) ->
                                actions.put(name, action));
                assertThat(actions).containsOnlyKeys("search");
                assertThat(main.getToolNames()).isEmpty();
                int before = remote.lists.get();
                var result = actions.get("search").apply(param()).block();
                assertThat(result).isNotNull();
                assertThat(remote.lists.get()).isEqualTo(before + 1);
                assertThat(remote.calls.get()).isEqualTo(1);
                verify(policy, times(2)).checkDispatch(CLAIM);
                verify(artifacts).archiveToolResult(eq(CLAIM), any(), eq("travel:read"));
                assertThat(unselected.lists.get()).isZero();
                verify(credentials, never()).resolve("unused-token");
            }
        }
    }

    @ParameterizedTest
    @ValueSource(strings = {"input", "output", "description", "missing", "duplicate"})
    void liveContractDriftBlocksBeforeEffectEvenWhenOriginalInputRemainsValid(String change)
            throws Exception {
        try (var remote = new McpContractFixture()) {
            var release = McpContractFixture.release(remote.remote("hotel", ""));
            var plan = freeze(release);
            var policy = mock(ExecutionPolicyService.class);
            var artifacts = artifacts();
            var actions = new HashMap<String, Function<ToolCallParam, Mono<ToolResultBlock>>>();
            try (var contribution =
                    new McpToolContribution(
                            policy,
                            McpCapabilityDiscoveryTest.credentials(),
                            artifacts,
                            CLAIM,
                            release,
                            List.of("travel:read"),
                            new Toolkit(),
                            plan)) {
                contribution.contribute(
                        (name, description, schema, scope, action, interaction) ->
                                actions.put(name, action));
                drift(remote, change);
                assertThatThrownBy(() -> actions.get("search").apply(param()).block())
                        .isInstanceOf(Blocked.class)
                        .hasMessage("mcp_contract_changed");
                assertThat(remote.calls.get()).isZero();
                verifyNoInteractions(artifacts);
                // Only assembly dispatch passed. The native call boundary is never reached.
                verify(policy).checkDispatch(CLAIM);
            }
        }
    }

    @Test
    void newAttemptAssemblyRejectsDriftBeforeResumedToolCanBeRegistered() throws Exception {
        try (var remote = new McpContractFixture()) {
            var release = McpContractFixture.release(remote.remote("hotel", ""));
            var plan = freeze(release);
            drift(remote, "output");
            var actions = new HashMap<String, Function<ToolCallParam, Mono<ToolResultBlock>>>();
            try (var contribution =
                    new McpToolContribution(
                            mock(ExecutionPolicyService.class),
                            McpCapabilityDiscoveryTest.credentials(),
                            artifacts(),
                            CLAIM,
                            release,
                            List.of("travel:read"),
                            new Toolkit(),
                            plan)) {
                assertThatThrownBy(
                                () ->
                                        contribution.contribute(
                                                (name,
                                                        description,
                                                        schema,
                                                        scope,
                                                        action,
                                                        interaction) -> actions.put(name, action)))
                        .isInstanceOf(Blocked.class)
                        .hasMessage("mcp_contract_changed");
                assertThat(actions).isEmpty();
                assertThat(remote.calls.get()).isZero();
            }
        }
    }

    @Test
    void revokedDispatchAfterFreshSchemaCheckCannotReachNativeEffect() throws Exception {
        try (var remote = new McpContractFixture()) {
            var release = McpContractFixture.release(remote.remote("hotel", ""));
            var plan = freeze(release);
            var policy = mock(ExecutionPolicyService.class);
            doNothing().doThrow(new Blocked("grant_changed")).when(policy).checkDispatch(CLAIM);
            var actions = new HashMap<String, Function<ToolCallParam, Mono<ToolResultBlock>>>();
            try (var contribution =
                    new McpToolContribution(
                            policy,
                            McpCapabilityDiscoveryTest.credentials(),
                            artifacts(),
                            CLAIM,
                            release,
                            List.of("travel:read"),
                            new Toolkit(),
                            plan)) {
                contribution.contribute(
                        (name, description, schema, scope, action, interaction) ->
                                actions.put(name, action));
                assertThatThrownBy(() -> actions.get("search").apply(param()).block())
                        .isInstanceOf(Blocked.class)
                        .hasMessage("grant_changed");
                assertThat(remote.calls.get()).isZero();
            }
        }
    }

    private static AgentExecutionPlan freeze(ReleaseDefinition release) {
        var bindings =
                new McpCapabilityDiscovery(McpCapabilityDiscoveryTest.credentials())
                        .discover(
                                new Owner("tenant", "subject"),
                                release,
                                List.of("mcp:hotel/search"));
        return new AgentExecutionPlan(
                "primary", "a".repeat(64), List.of("mcp:hotel/search"), bindings, release);
    }

    private static void drift(McpContractFixture remote, String change) {
        remote.tools =
                switch (change) {
                    case "input" -> List.of(McpContractFixture.tool("search", 200, "string"));
                    case "output" -> List.of(McpContractFixture.tool("search", 100, "integer"));
                    case "description" -> {
                        var changed =
                                new LinkedHashMap<>(
                                        McpContractFixture.tool("search", 100, "string"));
                        changed.put("description", "Changed description");
                        yield List.of(changed);
                    }
                    case "missing" -> List.of(McpContractFixture.tool("unused", 100, "string"));
                    case "duplicate" ->
                            List.of(
                                    McpContractFixture.tool("search", 100, "string"),
                                    McpContractFixture.tool("search", 200, "string"));
                    default -> throw new AssertionError("unsupported fixture mode");
                };
    }

    private static ToolCallParam param() {
        var input = Map.<String, Object>of("city", "Paris");
        return ToolCallParam.builder()
                .toolUseBlock(ToolUseBlock.builder().id("call").name("search").input(input).build())
                .input(input)
                .build();
    }

    private static ArtifactService artifacts() {
        var artifacts = mock(ArtifactService.class);
        when(artifacts.archiveToolResult(any(), any(), anyString()))
                .thenReturn(
                        new Artifact(
                                "artifact",
                                "turn",
                                "conversation",
                                "tenant",
                                "subject",
                                "release",
                                "a".repeat(64),
                                "{}",
                                2));
        return artifacts;
    }
}
