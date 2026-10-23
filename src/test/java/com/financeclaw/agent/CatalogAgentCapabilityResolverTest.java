package com.financeclaw.agent;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.Mockito.*;

import com.financeclaw.app.release.AgentPlanValidator;
import com.financeclaw.app.release.ReleaseService;
import com.financeclaw.app.release.port.Credentials;
import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.agent.*;
import com.financeclaw.domain.release.*;
import com.financeclaw.infrastructure.agent.CatalogAgentCapabilityResolver;
import com.financeclaw.infrastructure.release.BusinessConfigurationLoader;
import java.util.List;
import org.junit.jupiter.api.Test;

class CatalogAgentCapabilityResolverTest {
    private static final Owner OWNER = new Owner("tenant", "subject");
    private static final String ID = "19a37cb0-fd86-4b59-8bdb-405150f32fe0";
    private final Credentials credentials = mock(Credentials.class);
    private final ReleaseService releases = mock(ReleaseService.class);

    @Test
    void emptySelectionRemovesAllCapabilitiesAndDoesNotResolveUnusedMcpCredentials() {
        var base = base(ReleaseDefinition.SCOPES, List.of(remote(List.of())), null);
        var plan = resolver(base).resolve(OWNER, draft("primary", List.of()));
        assertThat(plan.capabilities()).isEmpty();
        assertThat(plan.definition().scopes()).isEmpty();
        assertThat(plan.definition().business().remotes()).isEmpty();
        assertThat(plan.runtimeArtifactHash()).matches("[0-9a-f]{64}");
        AgentPlanValidator.validate(plan, base, OWNER.tenant());
        verifyNoInteractions(credentials);
    }

    @Test
    void functionSelectionFreezesRequiredScopesAndArtifactSupportOnly() {
        var base = base(ReleaseDefinition.SCOPES, List.of(remote(List.of())), null);
        var plan =
                resolver(base).resolve(OWNER, draft("primary", List.of("function:search_catalog")));
        assertThat(plan.capabilities())
                .extracting(CapabilityBinding::id)
                .containsExactly("function:read_artifact_range", "function:search_catalog");
        assertThat(plan.definition().scopes()).containsExactly("artifact:read", "catalog:read");
        assertThat(plan.definition().business().remotes()).isEmpty();
        AgentPlanValidator.validate(plan, base, OWNER.tenant());
        verifyNoInteractions(credentials);
    }

    @Test
    void subAgentDependencyAndSkillContentAreIncludedInPublishedClosure() {
        var base = base(ReleaseDefinition.SCOPES, List.of(), null);
        var plan =
                resolver(base)
                        .resolve(
                                OWNER,
                                draft(
                                        "primary",
                                        List.of(
                                                "function:market_research",
                                                "skill:cocktail-from-what-i-have")));
        assertThat(plan.capabilities())
                .extracting(CapabilityBinding::id)
                .containsExactly(
                        "function:market_research",
                        "function:market_snapshot",
                        "function:read_artifact_range",
                        "skill:cocktail-from-what-i-have");
        assertThat(plan.definition().scopes()).contains("research:run", "market:read", "skill:use");
        assertThat(plan.capabilities().getLast().contractJson())
                .contains("cocktail-from-what-i-have");
        AgentPlanValidator.validate(plan, base, OWNER.tenant());
    }

    @Test
    void catalogIsOwnerApplicableMetadataAndNeverProbesRemoteServices() {
        var base =
                base(
                        List.of("travel:read", "catalog:read"),
                        List.of(remote(List.of("other"))),
                        null);
        var resolver = resolver(base);
        var catalog = resolver.catalog(OWNER, "a".repeat(64));
        assertThat(catalog.modelProfiles()).containsExactly("primary");
        assertThat(catalog.capabilities())
                .extracting(CapabilityOption::id)
                .doesNotContain("mcp:hotel/searchHotels", "function:search_catalog");
        assertThatThrownBy(
                        () ->
                                resolver.resolve(
                                        OWNER,
                                        draft("primary", List.of("function:search_catalog"))))
                .isInstanceOf(Blocked.class)
                .hasMessage("agent_capability_unavailable");
        assertThatThrownBy(
                        () ->
                                resolver.resolve(
                                        OWNER, draft("primary", List.of("mcp:hotel/searchHotels"))))
                .isInstanceOf(Blocked.class)
                .hasMessage("agent_capability_unavailable");
        verifyNoInteractions(credentials);
    }

    @Test
    void fallbackChoiceUsesOnlyTheFixedBaseProfileAndRejectsMissingProfiles() {
        var fallback =
                new ModelProfile("backup", "https://example.com/v1", 8000, 16000, 1000, 100, false);
        var base = base(ReleaseDefinition.SCOPES, List.of(), fallback);
        var resolver = resolver(base);
        var plan = resolver.resolve(OWNER, draft("fallback", List.of()));
        assertThat(plan.definition().primary()).isEqualTo(fallback);
        assertThat(plan.definition().fallback()).isNull();
        assertThat(plan.definition().maxModelCalls()).isEqualTo(base.maxModelCalls());
        AgentPlanValidator.validate(plan, base, OWNER.tenant());
        resolver = resolver(base(ReleaseDefinition.SCOPES, List.of(), null));
        var missing = resolver;
        assertThatThrownBy(() -> missing.resolve(OWNER, draft("fallback", List.of())))
                .hasMessage("agent_model_unavailable");
    }

    private CatalogAgentCapabilityResolver resolver(ReleaseDefinition base) {
        when(releases.require("a".repeat(64))).thenReturn(base);
        return new CatalogAgentCapabilityResolver(releases, credentials);
    }

    private static AgentDraft draft(String model, List<String> ids) {
        return new AgentDraft(
                ID, 1, "name", "prompt", "a".repeat(64), new AgentComposition(model, ids));
    }

    private static ReleaseDefinition base(
            List<String> scopes, List<RemoteEndpoint> remotes, ModelProfile fallback) {
        var defaults = BusinessConfigurationLoader.defaults();
        return new ReleaseDefinition(
                "base",
                "finance_agent",
                scopes,
                ReleaseDefinition.OFFLINE,
                fallback,
                12,
                32,
                new BusinessFeatures(remotes, defaults.skillHash(), "{}", null));
    }

    private static RemoteEndpoint remote(List<String> tenants) {
        return new RemoteEndpoint(
                "hotel",
                "http://127.0.0.1:1/mcp",
                List.of("127.0.0.1:1"),
                "DO_NOT_READ",
                false,
                tenants,
                List.of("searchHotels"),
                100);
    }
}
