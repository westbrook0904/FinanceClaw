package com.financeclaw.architecture;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

import com.financeclaw.app.execution.*;
import com.financeclaw.app.market.*;
import com.financeclaw.app.portfolio.*;
import com.financeclaw.app.portfolio.port.PortfolioPublication;
import com.financeclaw.domain.*;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;
import com.financeclaw.domain.market.MarketSnapshot;
import com.financeclaw.domain.portfolio.Portfolio;
import com.financeclaw.domain.research.ResearchRun;
import com.financeclaw.infrastructure.agentscope.NativeToolCalls;
import com.financeclaw.infrastructure.agentscope.capability.*;
import com.financeclaw.support.*;
import java.math.BigDecimal;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.util.*;
import java.util.function.Supplier;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.*;

class PortfolioResearchRulesTest {
    static Map<String, Object> baseline() throws Exception {
        try (var in =
                PortfolioResearchRulesTest.class.getResourceAsStream(
                        "/architecture/portfolio-research-baseline.json")) {
            return PayloadJson.object(
                    new String(Objects.requireNonNull(in).readAllBytes(), StandardCharsets.UTF_8));
        }
    }

    static String originalBytes(String key) throws Exception {
        try (var in =
                PortfolioResearchRulesTest.class.getResourceAsStream(
                        "/architecture/portfolio-research-baseline.json")) {
            return new com.fasterxml.jackson.databind.ObjectMapper()
                    .readTree(in)
                    .get(key)
                    .toString();
        }
    }

    static Claim claim() {
        return new Claim(
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
                        2,
                        null,
                        null));
    }

    static ExecutionIdentity identity() {
        return new ExecutionIdentity(
                "tenant",
                "subject",
                "conversation",
                "turn",
                "attempt",
                "release",
                1,
                List.of("market:read", "research:run", "report:write"),
                "2026-10-16 02:00:00+00:00");
    }

    static Portfolio.Holding holding(String price, String quantity, String currency, String time) {
        return new Portfolio.Holding(
                new MarketSnapshot("AAPL", new BigDecimal(price), currency, "fixture", time),
                new BigDecimal(quantity));
    }

    static Portfolio portfolio() {
        return new Portfolio("test", List.of(Portfolio.Position.parse("AAPL", "1", "0")), 48);
    }

    static ResearchRun research() {
        return new ResearchRun(
                claim().scope(), "call-1", identity(), List.of("aapl", "MSFT", "AAPL"));
    }

    @Test
    void originalSchemasScopeAndApprovalBytesAreUnchanged() throws Exception {
        var old = baseline();
        assertThat(
                        PayloadJson.canonical(
                                Map.of(
                                        "portfolio",
                                        PortfolioInputs.SCHEMA,
                                        "publish",
                                        PortfolioInputs.PUBLISH,
                                        "market",
                                        MarketToolSchemas.SNAPSHOT,
                                        "research",
                                        MarketToolSchemas.RESEARCH,
                                        "research_answer",
                                        MarketToolSchemas.ANSWER)))
                .isEqualTo(PayloadJson.canonical(old.get("schemas")));
        assertThat(PayloadJson.canonical(research().scope()))
                .isEqualTo(originalBytes("child_scope"));
        var draft = (Map<?, ?>) old.get("prepared");
        assertThat(
                        PayloadJson.canonical(
                                Map.of(
                                        "publish_call",
                                        NativeToolCalls.call(
                                                "publish",
                                                "publish_portfolio_report",
                                                Map.of("draft_hash", draft.get("draft_hash"))))))
                .isEqualTo("{\"publish_call\":" + originalBytes("publish_call") + "}");
    }

    @Test
    @SuppressWarnings("unchecked")
    void everyOriginalPortfolioStepInputAndDraftByteIsUnchanged() throws Exception {
        var old = baseline();
        var claim = claim();
        var policies = mock(ExecutionPolicyService.class);
        when(policies.scopes(claim)).thenReturn(List.of("market:read"));
        when(policies.identity(claim)).thenReturn(identity());
        var market = new MarketDataService(policies, hash -> (String) old.get("market"));
        var steps = mock(CapabilityStepService.class);
        var captured = new TreeMap<String, Object>();
        when(steps.run(eq(claim), eq(PortfolioReviewService.NAMESPACE), anyString(), any(), any()))
                .thenAnswer(
                        a -> {
                            Object result = ((Supplier<?>) a.getArgument(4)).get();
                            captured.put(
                                    a.getArgument(2),
                                    Map.of(
                                            "input_hash",
                                            PayloadDigest.sha256(
                                                    PayloadJson.canonical(a.getArgument(3))),
                                            "result",
                                            result));
                            return result;
                        });
        var workflow =
                new PortfolioReviewService(
                        steps, policies, market, mock(PortfolioPublication.class));
        var request = PortfolioInputs.parse((Map<String, Object>) old.get("input"));
        assertThat(PayloadJson.canonical(request.payload()))
                .isEqualTo(PayloadJson.canonical(old.get("normalized")));
        assertThat(PayloadJson.canonical(workflow.prepare(claim, request)))
                .isEqualTo(PayloadJson.canonical(old.get("prepared")));
        assertThat(PayloadJson.canonical(captured))
                .isEqualTo(PayloadJson.canonical(old.get("steps")));
    }

    @ParameterizedTest
    @ValueSource(strings = {"-1", "0", "0.000000001", "1000000000000000000000000", "1e2147483647"})
    void invalidQuantitiesFailWithoutContainer(String quantity) {
        assertThatThrownBy(() -> Portfolio.Position.parse("AAPL", quantity, "0"))
                .hasMessage("invalid_portfolio_decimal");
    }

    @Test
    void positionsNormalizeAndDefensivelyCopyWithoutLosingDecimalPrecision() {
        var positions =
                new ArrayList<>(List.of(Portfolio.Position.parse("aapl", "0.000000010", "0")));
        var p = new Portfolio("test", positions, 48);
        positions.clear();
        assertThat(p.positions()).hasSize(1);
        assertThat(p.positions().getFirst().quantity().toPlainString()).isEqualTo("0.00000001");
        assertThat(p.positions().getFirst().symbol()).isEqualTo("AAPL");
        assertThatThrownBy(
                        () ->
                                new Portfolio(
                                        "test",
                                        List.of(
                                                Portfolio.Position.parse("aapl", 1, 0),
                                                Portfolio.Position.parse("AAPL", 2, 0)),
                                        48))
                .hasMessage("duplicate_portfolio_symbol");
    }

    @ParameterizedTest
    @CsvSource({
        "'50,50',100.00,0.5000,high",
        "'30,25,25,20',100.00,0.3000,moderate",
        "'29,24,24,23',100.00,0.2900,low",
        "'0.005,0.010',0.02,0.6667,high"
    })
    void exposureRetainsHalfEvenAndExactThresholds(
            String prices, String total, String weight, String risk) {
        var e =
                Portfolio.Exposure.analyze(
                        Arrays.stream(prices.split(","))
                                .map(p -> holding(p, "1", "USD", "2026-10-16T00:00:00Z"))
                                .toList());
        assertThat(e.totalMarketValue().toPlainString()).isEqualTo(total);
        assertThat(e.largestPositionWeight().toPlainString()).isEqualTo(weight);
        assertThat(e.riskBand()).isEqualTo(risk);
    }

    @Test
    void freshnessIsInclusiveAtBothBoundariesAndRequiresOneCurrency() {
        var clock = Instant.parse("2026-10-16T00:00:00Z");
        var oldest = holding("1", "1", "USD", "2026-10-14T00:00:00Z");
        var future = holding("1", "1", "USD", "2026-10-16T00:05:00Z");
        assertThat(portfolio().oldestSnapshot(List.of(oldest, future), clock))
                .isEqualTo(oldest.snapshot().time());
        for (String time :
                List.of("2026-10-13T23:59:59.999999999Z", "2026-10-16T00:05:00.000000001Z"))
            assertThatThrownBy(
                            () ->
                                    portfolio()
                                            .oldestSnapshot(
                                                    List.of(holding("1", "1", "USD", time)), clock))
                    .hasMessage("stale_market_snapshot");
        assertThatThrownBy(
                        () ->
                                portfolio()
                                        .oldestSnapshot(
                                                List.of(
                                                        oldest,
                                                        holding(
                                                                "1",
                                                                "1",
                                                                "CNY",
                                                                "2026-10-16T00:00:00Z")),
                                                clock))
                .hasMessage("mixed_portfolio_currency");
    }

    @Test
    void evidencePreservesScaleAndRejectsInvalidPricesAndProvenance() {
        assertThat(
                        new MarketSnapshot(
                                        "AAPL",
                                        new BigDecimal("100.00"),
                                        "USD",
                                        "fixture",
                                        "2026-10-16T00:00:00Z")
                                .price()
                                .toPlainString())
                .isEqualTo("100.00");
        for (String price : List.of("0", "-1", "0.000000001", "1e30"))
            assertThatThrownBy(
                            () ->
                                    new MarketSnapshot(
                                            "AAPL",
                                            new BigDecimal(price),
                                            "USD",
                                            "fixture",
                                            "2026-10-16T00:00:00Z"))
                    .hasMessage("invalid_market_snapshot");
        assertThatThrownBy(
                        () ->
                                new MarketSnapshot(
                                        "AAPL",
                                        BigDecimal.ONE,
                                        "usd",
                                        "fixture",
                                        "2026-10-16T00:00:00Z"))
                .hasMessage("invalid_market_snapshot");
        assertThatThrownBy(
                        () ->
                                new MarketSnapshot(
                                        "AAPL", BigDecimal.ONE, "USD", "", "2026-10-16T00:00:00Z"))
                .hasMessage("invalid_market_snapshot");
    }

    @Test
    void childIdentityAndSnapshotScopeCanOnlyNarrowTheRoot() {
        var run = research();
        assertThat(run.identity().scopes()).containsExactly("market:read");
        assertThat(run.identity().turnId()).isEqualTo(identity().turnId());
        assertThat(run.identity().grantRevision()).isEqualTo(identity().grantRevision());
        assertThat(run.symbols()).containsExactly("AAPL", "MSFT");
        assertThat(run.validateSnapshot("aapl")).isEqualTo("AAPL");
        assertThat(run.validateSnapshot("MSFT")).isEqualTo("MSFT");
        for (Object symbol : Arrays.asList(null, "", "NVDA", List.of("AAPL"))) {
            assertThatThrownBy(() -> run.validateSnapshot(symbol))
                    .hasMessage("child_scope_expansion");
        }
        assertThatThrownBy(() -> run.recordEvidence("NVDA", Map.of("symbol", "NVDA")))
                .hasMessage("child_scope_expansion");
    }

    @Test
    void childEvidenceCopiesSnapshotsAndRetainsTheRequestedSymbolOrder() {
        var run = research();
        var snapshot = new HashMap<String, Object>();
        snapshot.put("symbol", "AAPL");
        snapshot.put("provider", "original");
        run.recordEvidence("MSFT", Map.of("symbol", "MSFT"));
        run.recordEvidence("AAPL", snapshot);
        snapshot.put("provider", "mutated");

        var evidence = run.completedEvidence(true, "complete");
        assertThat(evidence).extracting(s -> s.get("symbol")).containsExactly("AAPL", "MSFT");
        assertThat(evidence.getFirst()).containsEntry("provider", "original");
        assertThatThrownBy(() -> evidence.getFirst().put("provider", "changed"))
                .isInstanceOf(UnsupportedOperationException.class);
        assertThatThrownBy(() -> run.symbols().add("NVDA"))
                .isInstanceOf(UnsupportedOperationException.class);
    }

    @Test
    void childCompletionRequiresAllEvidenceTerminalStateAndBoundedText() {
        var run = research();
        run.recordEvidence("MSFT", Map.of("symbol", "MSFT"));
        assertThatThrownBy(() -> run.completedEvidence(true, "done"))
                .hasMessage("child_incomplete");
        run.recordEvidence("AAPL", Map.of("symbol", "AAPL"));
        assertThat(run.completedEvidence(true, "done"))
                .extracting(s -> s.get("symbol"))
                .containsExactly("AAPL", "MSFT");
        assertThatThrownBy(() -> run.completedEvidence(false, "done"))
                .hasMessage("child_incomplete");
        for (String text : List.of(" ", "a".repeat(8001)))
            assertThatThrownBy(() -> run.completedEvidence(true, text))
                    .hasMessage("child_incomplete");
    }

    @Test
    void domainNameLengthMatchesNativeUnicodeSchemaAndRetainsNumericInputRepresentation() {
        var input =
                Map.<String, Object>of(
                        "portfolio_name",
                        "🧮".repeat(70),
                        "positions",
                        List.of(Map.of("symbol", "AAPL", "quantity", 1, "cost_basis", 0)),
                        "max_snapshot_age_hours",
                        48.0);
        assertThat(
                        io.agentscope.core.tool.ToolValidator.validateInput(
                                PayloadJson.canonical(input), PortfolioInputs.SCHEMA))
                .isNull();
        var parsed = PortfolioInputs.parse(input);
        assertThat(parsed.portfolio().name()).isEqualTo(input.get("portfolio_name"));
        assertThat(PayloadJson.canonical(parsed.payload()))
                .contains("\"max_snapshot_age_hours\":48.0");
    }
}
