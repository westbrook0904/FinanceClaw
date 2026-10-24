package com.financeclaw.app.market;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.Mockito.*;

import com.financeclaw.app.execution.ExecutionPolicyService;
import com.financeclaw.app.market.port.MarketCatalog;
import com.financeclaw.domain.Task.*;
import com.financeclaw.domain.agent.*;
import com.financeclaw.domain.release.*;
import com.financeclaw.support.PayloadDigest;
import com.financeclaw.support.PayloadJson;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;

/** 目标行情直接来自已校验的冻结发布；不借用父发布数据，不新增每次读取的发布查询。 */
class MarketDataServiceTest {
    private static final String ID = "11e8e638-0689-40df-99ed-b22de07c1172";
    private static final String HASH = "b".repeat(64);
    private static final Claim PARENT =
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
                            "parent-release",
                            "running",
                            1,
                            null,
                            null));
    private final ExecutionPolicyService policies = mock(ExecutionPolicyService.class);
    private final MarketCatalog catalog = mock(MarketCatalog.class);
    private final MarketDataService market = new MarketDataService(policies, catalog);

    @Test
    void privateReadUsesTheTargetSnapshotAndBindsTheSourceHashWhileChargingTheRealParent() {
        var target = new PrivateAgentTarget(ID, HASH, PrivateAgentTarget.Profile.MARKET_SNAPSHOT);
        var result =
                market.readFrozen(PARENT, target, frozen("AAPL", row("leaf", "200.00")), "aapl");
        assertThat(result)
                .containsEntry("symbol", "AAPL")
                .containsEntry("provider", "leaf")
                .containsEntry("source_release_hash", HASH)
                .containsEntry(
                        "input_hash",
                        PayloadDigest.sha256(
                                PayloadJson.canonical(
                                        Map.of("symbol", "AAPL", "source_release_hash", HASH))));
        verify(policies).consumeTool(PARENT, "market:read");
        verifyNoInteractions(catalog);
    }

    @Test
    void inheritedOrWrongProfileCannotReadTargetDataOrConsumeRootBudget() {
        var target = new PrivateAgentTarget(ID, HASH, PrivateAgentTarget.Profile.MARKET_SNAPSHOT);
        var valid = frozen("AAPL", row("leaf", "200.00"));
        var inherited =
                new ResolvedAgentRelease(ID, "leaf", valid.definition(), "tenant", "subject");
        assertThatThrownBy(() -> market.readFrozen(PARENT, target, inherited, "AAPL"))
                .hasMessage("agent_leaf_required");
        assertThatThrownBy(
                        () ->
                                market.readFrozen(
                                        PARENT, new PrivateAgentTarget(ID, HASH), valid, "AAPL"))
                .hasMessage("agent_leaf_required");
        verifyNoInteractions(policies, catalog);
    }

    @Test
    void largestLegalSnapshotBodyIncludingFrozenHashStaysBelowTheSmallResultBoundary() {
        // 控制字符是 provider 目前允许的最坏 JSON 转义长度，覆盖 128 个字符，而非只测 ASCII 常用值。
        String provider = "\u0001".repeat(128);
        var row =
                Map.<String, Object>of(
                        "price",
                        "9999999999999999.99999999",
                        "currency",
                        "USD",
                        "provider",
                        provider,
                        "as_of",
                        "+999999999-12-31T23:59:59.999999999+18:00");
        var target = new PrivateAgentTarget(ID, HASH, PrivateAgentTarget.Profile.MARKET_SNAPSHOT);
        var result =
                market.readFrozen(
                        PARENT, target, frozen("ABCDEFGHIJKLMNOP", row), "ABCDEFGHIJKLMNOP");
        assertThat(result)
                .containsEntry("provider", provider)
                .containsEntry("source_release_hash", HASH);
        // SDK 的 ID/name/ToolResultBlock 包装另由实际运行时测试验证；这里证明完整业务正文的上界。
        assertThat(PayloadJson.canonical(result).getBytes(StandardCharsets.UTF_8).length)
                .isLessThan(2048);
        verifyNoInteractions(catalog);
    }

    private static Map<String, Object> row(String provider, String price) {
        return Map.of(
                "price",
                price,
                "currency",
                "USD",
                "provider",
                provider,
                "as_of",
                "2026-10-24T00:00:00Z");
    }

    private static ResolvedAgentRelease frozen(String symbol, Map<String, Object> snapshot) {
        var definition =
                new ReleaseDefinition(
                        "v1",
                        "finance_agent",
                        List.of("market:read"),
                        ReleaseDefinition.OFFLINE,
                        null,
                        4,
                        1,
                        new BusinessFeatures(
                                List.of(),
                                "skills",
                                PayloadJson.canonical(Map.of(symbol, snapshot)),
                                null));
        var snapshotBinding =
                new CapabilityBinding(
                        PrivateAgentTarget.SNAPSHOT,
                        "function",
                        "market_snapshot",
                        List.of("market:read"),
                        List.of(PrivateAgentTarget.READER),
                        "{}",
                        HASH);
        var reader =
                new CapabilityBinding(
                        PrivateAgentTarget.READER,
                        "function",
                        "read_artifact_range",
                        List.of(),
                        List.of(),
                        "{}",
                        HASH);
        return new ResolvedAgentRelease(
                ID,
                "leaf",
                definition,
                "tenant",
                "subject",
                new AgentExecutionPlan(
                        "primary",
                        HASH,
                        List.of(PrivateAgentTarget.SNAPSHOT),
                        List.of(snapshotBinding, reader),
                        definition));
    }
}
