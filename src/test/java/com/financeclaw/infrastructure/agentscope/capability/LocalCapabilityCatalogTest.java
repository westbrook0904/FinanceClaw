package com.financeclaw.infrastructure.agentscope.capability;

import static org.assertj.core.api.Assertions.*;

import com.financeclaw.domain.agent.CapabilityBinding;
import com.financeclaw.domain.release.*;
import com.financeclaw.support.*;
import java.util.*;
import java.util.function.Function;
import java.util.stream.Collectors;
import org.junit.jupiter.api.Test;

class LocalCapabilityCatalogTest {
    @Test
    void dependenciesDescribeInternalMarketReadsAndPreparedPortfolioPublication() {
        var bindings =
                LocalCapabilityCatalog.bindings(
                                base(
                                        List.of(
                                                "market:read",
                                                "research:run",
                                                "workflow:run",
                                                "report:write",
                                                "artifact:read",
                                                "catalog:read")))
                        .stream()
                        .collect(Collectors.toMap(CapabilityBinding::id, Function.identity()));
        assertThat(bindings.get("function:search_catalog").scopes())
                .containsExactlyInAnyOrder("artifact:read", "catalog:read");
        assertThat(bindings.get("function:market_research").dependencies())
                .contains("function:market_snapshot", LocalCapabilityCatalog.ARTIFACT_READER);
        assertThat(bindings.get("function:portfolio_review").dependencies())
                .contains("function:market_snapshot");
        assertThat(bindings.get("function:publish_portfolio_report").dependencies())
                .contains("function:portfolio_review");
        assertThat(bindings.get(LocalCapabilityCatalog.ARTIFACT_READER).dependencies()).isEmpty();
        for (var binding : bindings.values()) {
            binding.validate();
            assertThat(binding.contentHash())
                    .isEqualTo(PayloadDigest.sha256(binding.contractJson()));
            assertThat(PayloadJson.object(binding.contractJson()))
                    .containsKeys("inputSchema", "humanAnswerSchema", "approval", "description");
            if (!binding.id().equals(LocalCapabilityCatalog.ARTIFACT_READER)) {
                assertThat(binding.dependencies()).contains(LocalCapabilityCatalog.ARTIFACT_READER);
            }
        }
    }

    @Test
    void directoryExcludesFunctionsWhoseInternalScopesAreUnavailable() {
        assertThat(
                        LocalCapabilityCatalog.bindings(
                                base(List.of("catalog:read", "research:run", "workflow:run"))))
                .extracting(CapabilityBinding::id)
                .doesNotContain(
                        "function:search_catalog",
                        "function:market_research",
                        "function:portfolio_review");
    }

    private static ReleaseDefinition base(List<String> scopes) {
        return new ReleaseDefinition(
                "test",
                "finance_agent",
                scopes,
                ReleaseDefinition.OFFLINE,
                null,
                12,
                20,
                new BusinessFeatures(List.of(), "", "{}", null));
    }
}
