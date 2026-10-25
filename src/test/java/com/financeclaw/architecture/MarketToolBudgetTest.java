package com.financeclaw.architecture;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.Mockito.*;

import com.financeclaw.app.artifact.ArtifactService;
import com.financeclaw.app.execution.*;
import com.financeclaw.app.execution.port.*;
import com.financeclaw.app.market.MarketDataService;
import com.financeclaw.app.market.port.MarketCatalog;
import com.financeclaw.app.portfolio.*;
import com.financeclaw.app.portfolio.port.PortfolioPublication;
import com.financeclaw.app.release.ReleaseService;
import com.financeclaw.app.release.port.Credentials;
import com.financeclaw.app.report.ReportService;
import com.financeclaw.app.research.MarketResearchService;
import com.financeclaw.app.task.TaskEventHub;
import com.financeclaw.app.task.port.*;
import com.financeclaw.domain.ExecutionPolicy;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.StepResult;
import com.financeclaw.domain.Task.*;
import com.financeclaw.domain.agent.AgentExecutionPlan;
import com.financeclaw.domain.release.*;
import com.financeclaw.infrastructure.agentscope.NativeToolCalls;
import com.financeclaw.infrastructure.agentscope.RuntimeArtifacts;
import com.financeclaw.infrastructure.agentscope.capability.*;
import com.financeclaw.infrastructure.agentscope.execution.GovernedTools;
import com.financeclaw.support.PayloadDigest;
import com.financeclaw.support.PayloadJson;
import io.agentscope.core.message.*;
import io.agentscope.core.tool.ToolCallParam;
import java.time.Duration;
import java.util.*;
import java.util.function.Supplier;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.support.SimpleTransactionStatus;
import org.springframework.transaction.support.TransactionTemplate;

/** 通过真实本地工具包装器与用例验证扣额归属，持久层替身保留输入摘要和回执语义。 */
class MarketToolBudgetTest {
    private static final Claim CLAIM =
            new Claim(
                    new Command("command", "turn", "running", "attempt", 1, true, null),
                    new Turn(
                            "turn",
                            "conversation",
                            "tenant",
                            "alice",
                            "key",
                            "hash",
                            "message",
                            "release",
                            "running",
                            1,
                            null,
                            null));
    private static final List<String> SCOPES =
            List.of("market:read", "research:run", "workflow:run");
    private static final String SNAPSHOTS =
            "{\"AAPL\":{\"price\":\"200.00\",\"currency\":\"USD\",\"provider\":\"fixture\",\"as_of\":\"2026-10-22T00:00:00Z\"}}";
    private final TaskQueryGateway reads = mock(TaskQueryGateway.class);
    private final ExecutionPolicyGateway governance = mock(ExecutionPolicyGateway.class);
    private final CommandGateway commands = mock(CommandGateway.class);
    private final ExecutionMemory memory = mock(ExecutionMemory.class);
    private final ExecutionRelease executionReleases = mock(ExecutionRelease.class);
    private final ReleaseService releases = mock(ReleaseService.class);
    private final MarketCatalog catalog = mock(MarketCatalog.class);
    private final ArtifactService artifacts = mock(ArtifactService.class);
    private final CapabilityStepGateway storedSteps = mock(CapabilityStepGateway.class);
    private final Map<String, StepResult> receipts = new HashMap<>();
    private ExecutionPolicyService policies;
    private ExecutionLeaseService leases;
    private CapabilityStepService steps;
    private MarketDataService market;

    @BeforeEach
    void setup() {
        var manager = mock(PlatformTransactionManager.class);
        when(manager.getTransaction(any())).thenAnswer(ignored -> new SimpleTransactionStatus());
        leases = new ExecutionLeaseService(commands, null, null, new TransactionTemplate(manager));
        policies =
                new ExecutionPolicyService(
                        reads,
                        mock(TaskEventHub.class),
                        governance,
                        executionReleases,
                        memory,
                        leases);
        steps = new CapabilityStepService(storedSteps, leases, policies);
        market = new MarketDataService(policies, catalog);
        when(commands.lockCommand("command")).thenReturn(CLAIM.command());
        when(reads.turn("turn")).thenReturn(CLAIM.turn());
        when(governance.policy("turn"))
                .thenReturn(new Policy("turn", 4, PayloadJson.canonical(SCOPES), 0, 0));
        when(governance.lockGrant("conversation"))
                .thenReturn(new Grant("conversation", 4, PayloadJson.canonical(SCOPES)));
        when(governance.startedAt("attempt")).thenReturn("2026-10-22T01:00:00Z");
        when(executionReleases.requireForTurn(CLAIM.turn()))
                .thenReturn(new ExecutionPolicy(SCOPES, 10, 1));
        // 首次额度恰好为一；再次真实执行必须在读取行情或调用远程工具前被拒绝。
        when(governance.toolCall("turn", 1)).thenReturn(1, 0);
        when(catalog.snapshots("release")).thenReturn(SNAPSHOTS);
        when(storedSteps.step(eq("turn"), anyString(), anyString()))
                .thenAnswer(i -> receipts.get(i.getArgument(1) + "/" + i.getArgument(2)));
        doAnswer(
                        i -> {
                            receipts.put(
                                    i.getArgument(1) + "/" + i.getArgument(2),
                                    new StepResult(i.getArgument(3), i.getArgument(4)));
                            return null;
                        })
                .when(storedSteps)
                .saveStep(eq("turn"), anyString(), anyString(), anyString(), anyString());
    }

    @ParameterizedTest
    @ValueSource(booleans = {false, true})
    void mainAgentSnapshotSpendsOneSlotAndNextExecutionIsRejected(boolean composed) {
        var release = definition(List.of("market:read"), List.of());
        when(releases.require("release")).thenReturn(release);
        AgentExecutionPlan plan =
                composed
                        ? new AgentExecutionPlan(
                                "primary",
                                RuntimeArtifacts.hash(),
                                List.of("function:market_snapshot"),
                                LocalCapabilityCatalog.bindings(release).stream()
                                        .filter(
                                                b ->
                                                        b.name().equals("market_snapshot")
                                                                || b.id().equals(
                                                                                LocalCapabilityCatalog
                                                                                        .ARTIFACT_READER))
                                        .toList(),
                                release)
                        : null;
        try (var tools = tools(plan)) {
            tools.business(
                    null, new BusinessCapabilities(market, null, null, policies, null, null));
            var tool = tools.toolkit.getTool("market_snapshot");
            var result =
                    tool.callAsync(param("first", "market_snapshot", Map.of("symbol", "aapl")))
                            .block(Duration.ofSeconds(5));
            assertThat(text(result)).contains("AAPL", "fixture");
            assertThatThrownBy(
                            () ->
                                    tool.callAsync(
                                                    param(
                                                            "second",
                                                            "market_snapshot",
                                                            Map.of("symbol", "AAPL")))
                                            .block(Duration.ofSeconds(5)))
                    .isInstanceOf(Blocked.class)
                    .hasMessage("execution_budget_exhausted");
            verify(governance, times(2)).toolCall("turn", 1);
            verify(catalog).snapshots("release");
        }
    }

    @Test
    void localSnapshotStillRejectsInvalidInputAndCurrentAuthorizationBeforeReading() {
        when(releases.require("release")).thenReturn(definition(List.of("market:read"), List.of()));
        try (var tools = tools(null)) {
            tools.business(
                    null, new BusinessCapabilities(market, null, null, policies, null, null));
            var tool = tools.toolkit.getTool("market_snapshot");
            assertThatThrownBy(
                            () ->
                                    tool.callAsync(
                                                    param(
                                                            "invalid",
                                                            "market_snapshot",
                                                            Map.of("symbol", 42)))
                                            .block(Duration.ofSeconds(5)))
                    .hasMessage("invalid_tool_input");
            when(governance.lockGrant("conversation"))
                    .thenReturn(new Grant("conversation", 5, "[]"));
            assertThatThrownBy(
                            () ->
                                    tool.callAsync(
                                                    param(
                                                            "revoked",
                                                            "market_snapshot",
                                                            Map.of("symbol", "AAPL")))
                                            .block(Duration.ofSeconds(5)))
                    .hasMessage("authorization_changed");
            verifyNoInteractions(catalog);
            verify(governance, never()).toolCall(anyString(), anyInt());
        }
    }

    @Test
    void researchSnapshotRestoresChildReceiptWithoutAnotherReadOrRootReservation() {
        var children = mock(ChildInvocationService.class);
        var research = new MarketResearchService(policies, children, market);
        var input = Map.<String, Object>of("symbols", List.of("AAPL"), "analysis_period", "frozen");
        var invocation =
                new ChildInvocationService.Invocation(
                        CLAIM,
                        "research",
                        PayloadDigest.sha256(PayloadJson.canonical(input)),
                        null,
                        List.of());
        when(children.begin(CLAIM, "research", input)).thenReturn(invocation);
        when(children.snapshot(eq(invocation), eq("snapshot"), anyMap(), any()))
                .thenAnswer(i -> ((Supplier<?>) i.getArgument(3)).get());
        when(children.complete(eq(invocation), any()))
                .thenAnswer(i -> ((Supplier<?>) i.getArgument(1)).get());
        var first = research.begin(CLAIM, "research", input);
        var snapshot = research.snapshot(first, "snapshot", Map.of("symbol", "AAPL"));
        // 持久化服务是回放权威；此处只模拟其返回回执，不在测试内重建一套子预算计数。
        doReturn(snapshot).when(children).snapshot(eq(invocation), eq("snapshot"), anyMap(), any());
        var resumed = research.begin(CLAIM, "research", input);
        assertThat(research.snapshot(resumed, "snapshot", Map.of("symbol", "AAPL")))
                .isEqualTo(snapshot);
        var result = (Map<?, ?>) research.complete(resumed, true, "frozen evidence");
        assertThat(result.get("evidence")).isEqualTo(List.of(snapshot));
        verify(catalog).snapshots("release");
        verify(governance).toolCall("turn", 1);

        doThrow(new Blocked("authorization_changed"))
                .when(children)
                .snapshot(eq(invocation), eq("snapshot"), anyMap(), any());
        assertThatThrownBy(() -> research.snapshot(resumed, "snapshot", Map.of("symbol", "AAPL")))
                .hasMessage("authorization_changed");
        verify(catalog).snapshots("release");
        verify(governance).toolCall("turn", 1);
    }

    @Test
    void workflowSnapshotRemainsBudgetedOnceAndCachedPreparationDoesNotReserveAgain() {
        var workflow =
                new PortfolioReviewService(
                        steps, policies, market, mock(PortfolioPublication.class));
        var request =
                PortfolioRequest.from(
                        Map.of(
                                "portfolio_name",
                                "test",
                                "positions",
                                List.of(
                                        Map.of(
                                                "symbol",
                                                "AAPL",
                                                "quantity",
                                                "1",
                                                "cost_basis",
                                                "0"))));
        var first = workflow.prepare(CLAIM, request);
        assertThat(workflow.prepare(CLAIM, request)).isEqualTo(first);
        verify(catalog).snapshots("release");
        verify(governance).toolCall("turn", 1);
    }

    @Test
    void mcpToolWithTheSameNameStillReservesItsOwnSlot() throws Exception {
        try (var remote = new McpContractFixture()) {
            remote.tools = List.of(McpContractFixture.tool("market_snapshot", 100, "string"));
            var endpoint = remote.remote("hotel", "");
            var selected =
                    new RemoteEndpoint(
                            endpoint.service(),
                            endpoint.url(),
                            endpoint.allowedHosts(),
                            endpoint.credentialRef(),
                            endpoint.external(),
                            endpoint.tenants(),
                            List.of("market_snapshot"),
                            endpoint.timeoutMillis());
            when(releases.require("release"))
                    .thenReturn(definition(List.of("travel:read"), List.of(selected)));
            when(governance.policy("turn"))
                    .thenReturn(new Policy("turn", 4, "[\"travel:read\"]", 0, 0));
            when(governance.lockGrant("conversation"))
                    .thenReturn(new Grant("conversation", 4, "[\"travel:read\"]"));
            when(executionReleases.requireForTurn(CLAIM.turn()))
                    .thenReturn(new ExecutionPolicy(List.of("travel:read"), 10, 1));
            when(artifacts.archiveToolResult(eq(CLAIM), any(), eq("travel:read")))
                    .thenReturn(
                            new Artifact(
                                    "artifact",
                                    "turn",
                                    "conversation",
                                    "tenant",
                                    "alice",
                                    "release",
                                    "a".repeat(64),
                                    "{}",
                                    2));
            try (var tools = tools(null)) {
                var tool = tools.toolkit.getTool("market_snapshot");
                assertThat(
                                tool.callAsync(
                                                param(
                                                        "first",
                                                        "market_snapshot",
                                                        Map.of("city", "Paris")))
                                        .block(Duration.ofSeconds(5)))
                        .isNotNull();
                assertThatThrownBy(
                                () ->
                                        tool.callAsync(
                                                        param(
                                                                "second",
                                                                "market_snapshot",
                                                                Map.of("city", "Paris")))
                                                .block(Duration.ofSeconds(5)))
                        .hasMessage("execution_budget_exhausted");
                assertThat(remote.calls.get()).isEqualTo(1);
                verify(governance, times(2)).toolCall("turn", 1);
                verifyNoInteractions(catalog);
            }
        }
    }

    private GovernedTools tools(AgentExecutionPlan plan) {
        var credentials = mock(Credentials.class);
        // 与生产凭据端口一致：空引用返回空 token，不能使用 Mockito 默认的 null。
        when(credentials.resolve(anyString())).thenReturn("");
        return new GovernedTools(
                policies, releases, credentials, artifacts, mock(ReportService.class), CLAIM, plan);
    }

    private static ReleaseDefinition definition(List<String> scopes, List<RemoteEndpoint> remotes) {
        return new ReleaseDefinition(
                "test",
                "finance_agent",
                scopes,
                ReleaseDefinition.OFFLINE,
                null,
                10,
                1,
                new BusinessFeatures(remotes, "", SNAPSHOTS, null));
    }

    private static ToolCallParam param(String id, String name, Map<String, Object> input) {
        return ToolCallParam.builder()
                .toolUseBlock(NativeToolCalls.call(id, name, input))
                .input(input)
                .build();
    }

    private static String text(ToolResultBlock result) {
        return result.getOutput().stream()
                .filter(TextBlock.class::isInstance)
                .map(TextBlock.class::cast)
                .map(TextBlock::getText)
                .collect(java.util.stream.Collectors.joining("\n"));
    }
}
