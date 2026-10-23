package com.financeclaw.app.research;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.Mockito.*;

import com.financeclaw.app.execution.ChildInvocationService;
import com.financeclaw.app.execution.ExecutionPolicyService;
import com.financeclaw.app.market.MarketDataService;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;
import com.financeclaw.support.PayloadDigest;
import com.financeclaw.support.PayloadJson;
import java.util.List;
import java.util.Map;
import java.util.function.Supplier;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

/** 研究用例只负责业务范围和证据；子调用回执与计数以 ChildInvocationService 的返回值为准。 */
class MarketResearchServiceTest {
    private static final Claim CLAIM =
            new Claim(
                    new Command("command", "turn", "running", "attempt", 1, true, null),
                    new Turn(
                            "turn",
                            "conversation",
                            "tenant",
                            "subject",
                            "key",
                            "request",
                            "hello",
                            "release",
                            "running",
                            1,
                            null,
                            null));
    private static final Map<String, Object> INPUT =
            Map.of("symbols", List.of("aapl"), "analysis_period", "frozen");
    private static final Map<String, Object> SNAPSHOT =
            Map.of("symbol", "AAPL", "provider", "frozen provider");
    private static final String INPUT_HASH = PayloadDigest.sha256(PayloadJson.canonical(INPUT));
    private final ExecutionPolicyService policies = mock(ExecutionPolicyService.class);
    private final ChildInvocationService children = mock(ChildInvocationService.class);
    private final MarketDataService market = mock(MarketDataService.class);
    private final ChildInvocationService.Invocation invocation =
            new ChildInvocationService.Invocation(CLAIM, "research", INPUT_HASH, null, List.of());
    private final MarketResearchService research =
            new MarketResearchService(policies, children, market);

    @BeforeEach
    void setup() {
        when(children.begin(CLAIM, "research", INPUT)).thenReturn(invocation);
        when(policies.scopes(CLAIM)).thenReturn(List.of("market:read", "research:run"));
        when(policies.identity(CLAIM))
                .thenReturn(
                        new ExecutionIdentity(
                                "tenant",
                                "subject",
                                "conversation",
                                "turn",
                                "attempt",
                                "release",
                                4,
                                List.of("market:read", "research:run"),
                                "2026-10-23T00:00:00Z"));
    }

    @Test
    void freshBeginRetainsTheInvocationAndCreatesANarrowedResearchContext() {
        var execution = research.begin(CLAIM, "research", INPUT);

        assertThat(execution.invocation()).isSameAs(invocation);
        assertThat(execution.namespace()).isEqualTo("child:research");
        assertThat(execution.input()).isEqualTo(INPUT);
        assertThat(execution.cachedResult()).isNull();
        assertThat(execution.run().symbols()).containsExactly("AAPL");
        assertThat(execution.run().identity().scopes()).containsExactly("market:read");
        assertThat(execution.run().identity().grantRevision()).isEqualTo(4);
        verify(children).begin(CLAIM, "research", INPUT);
        verifyNoInteractions(market);
    }

    @Test
    void completedBeginReturnsTheAuthoritativeReceiptWithoutRebuildingRuntimeState() {
        var cached =
                new ChildInvocationService.Invocation(
                        CLAIM, "research", INPUT_HASH, "receipt", List.of());
        when(children.begin(CLAIM, "research", INPUT)).thenReturn(cached);

        var execution = research.begin(CLAIM, "research", INPUT);

        assertThat(execution.invocation()).isSameAs(cached);
        assertThat(execution.cachedResult()).isEqualTo("receipt");
        assertThat(execution.run()).isNull();
        verify(policies, never()).identity(any());
        verify(children, never()).reserveModel(any());
        verifyNoInteractions(market);
    }

    @Test
    void modelReservationsUseTheChildBoundaryAndPreserveItsRejection() {
        var execution = research.begin(CLAIM, "research", INPUT);
        research.modelCall(execution);
        verify(children).reserveModel(invocation);
        doThrow(new Blocked("child_model_budget_exceeded")).when(children).reserveModel(invocation);

        assertThatThrownBy(() -> research.modelCall(execution))
                .hasMessage("child_model_budget_exceeded");
        verify(policies, never()).consume(any(), anyBoolean());
        verify(policies, never()).consumeTool(any(), any());
        verifyNoInteractions(market);
    }

    @Test
    void cachedSnapshotRebuildsEvidenceWithoutEvaluatingTheMarketSupplier() {
        when(children.snapshot(eq(invocation), eq("snapshot"), anyMap(), any()))
                .thenReturn(SNAPSHOT);
        when(children.complete(eq(invocation), any()))
                .thenAnswer(i -> ((Supplier<?>) i.getArgument(1)).get());
        var restored = research.begin(CLAIM, "research", INPUT);

        assertThat(research.snapshot(restored, "snapshot", Map.of("symbol", "aapl")))
                .isEqualTo(SNAPSHOT);
        var result = (Map<?, ?>) research.complete(restored, true, "restored summary");

        assertThat(result.get("evidence")).isEqualTo(List.of(SNAPSHOT));
        assertThat(result.get("analysis_period")).isEqualTo("frozen");
        assertThat(result.get("summary")).isEqualTo("restored summary");
        verify(children)
                .snapshot(eq(invocation), eq("snapshot"), eq(Map.of("symbol", "aapl")), any());
        verify(children).complete(eq(invocation), any());
        verifyNoInteractions(market);
        verify(policies, never()).consumeTool(any(), any());
    }

    @Test
    void pendingBeginRestoresSuccessfulSnapshotsWithoutReadingMarketOrReplayingTools() {
        var restoredInvocation =
                new ChildInvocationService.Invocation(
                        CLAIM,
                        "research",
                        INPUT_HASH,
                        null,
                        List.of(PayloadJson.canonical(SNAPSHOT)));
        when(children.begin(CLAIM, "research", INPUT)).thenReturn(restoredInvocation);
        when(children.complete(eq(restoredInvocation), any()))
                .thenAnswer(i -> ((Supplier<?>) i.getArgument(1)).get());

        var restored = research.begin(CLAIM, "research", INPUT);
        var result = (Map<?, ?>) research.complete(restored, true, "checkpoint final answer");

        assertThat(result.get("evidence")).isEqualTo(List.of(SNAPSHOT));
        assertThat(result.get("summary")).isEqualTo("checkpoint final answer");
        verifyNoInteractions(market);
        verify(children, never()).snapshot(any(), anyString(), anyMap(), any());
        verify(children, never()).reserveModel(any());
        verify(policies, never()).consumeTool(any(), any());
    }

    @Test
    void restoredPartialEvidenceStillCannotCompleteTheMissingSymbols() {
        var input =
                Map.<String, Object>of(
                        "symbols", List.of("AAPL", "MSFT"), "analysis_period", "frozen");
        var restoredInvocation =
                new ChildInvocationService.Invocation(
                        CLAIM,
                        "research",
                        PayloadDigest.sha256(PayloadJson.canonical(input)),
                        null,
                        List.of(PayloadJson.canonical(SNAPSHOT)));
        when(children.begin(CLAIM, "research", input)).thenReturn(restoredInvocation);

        var restored = research.begin(CLAIM, "research", input);

        assertThatThrownBy(() -> research.complete(restored, true, "missing MSFT evidence"))
                .hasMessage("child_incomplete");
        verify(children, never()).complete(any(), any());
        verifyNoInteractions(market);
    }

    @Test
    void symbolExpansionIsRejectedBeforeTheChildCanReadOrReserveAnything() {
        var execution = research.begin(CLAIM, "research", INPUT);

        assertThatThrownBy(() -> research.snapshot(execution, "outside", Map.of("symbol", "NVDA")))
                .hasMessage("child_scope_expansion");

        verify(children, never()).snapshot(any(), anyString(), anyMap(), any());
        verifyNoInteractions(market);
    }

    @Test
    void childSnapshotRejectionCannotCreateCompletionEvidence() {
        when(children.snapshot(eq(invocation), eq("snapshot"), anyMap(), any()))
                .thenThrow(new Blocked("child_tool_budget_exceeded"));
        var execution = research.begin(CLAIM, "research", INPUT);

        assertThatThrownBy(() -> research.snapshot(execution, "snapshot", Map.of("symbol", "AAPL")))
                .hasMessage("child_tool_budget_exceeded");
        assertThatThrownBy(() -> execution.run().completedEvidence(true, "no actual evidence"))
                .hasMessage("child_incomplete");

        verifyNoInteractions(market);
        verify(policies, never()).consumeTool(any(), any());
    }

    @Test
    void checkpointRejectionCannotExecuteTheSdkStateOperation() {
        doThrow(new Fenced()).when(children).checkpoint(eq(invocation), any());
        var execution = research.begin(CLAIM, "research", INPUT);

        assertThatThrownBy(
                        () ->
                                research.checkpoint(
                                        execution,
                                        () -> {
                                            throw new AssertionError(
                                                    "A fenced child must not access SDK state");
                                        }))
                .isInstanceOf(Fenced.class);

        verify(children).checkpoint(eq(invocation), any());
        verifyNoInteractions(market);
    }
}
