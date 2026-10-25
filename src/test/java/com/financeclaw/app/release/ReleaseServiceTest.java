package com.financeclaw.app.release;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

import com.financeclaw.app.release.port.ReleaseRepository;
import com.financeclaw.app.release.port.StoredRelease;
import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.agent.AgentDraft;
import com.financeclaw.domain.agent.AgentExecutionPlan;
import com.financeclaw.domain.agent.AgentReleaseManifest;
import com.financeclaw.domain.agent.ComposedAgentReleaseManifest;
import com.financeclaw.domain.release.BusinessFeatures;
import com.financeclaw.domain.release.Deployment;
import com.financeclaw.domain.release.DeploymentChange;
import com.financeclaw.domain.release.ReleaseDefinition;
import java.util.List;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.support.SimpleTransactionStatus;
import org.springframework.transaction.support.TransactionTemplate;

class ReleaseServiceTest {
    private static final String PRIVATE_AGENT = "11111111-1111-4111-8111-111111111111";
    private final ReleaseRepository repository = mock(ReleaseRepository.class);
    private final ReleaseDefinition initial = definition("initial");
    private final String initialHash = ReleaseIdentity.hash(initial);
    private ReleaseService releases;

    @BeforeEach
    void setup() {
        var manager = mock(PlatformTransactionManager.class);
        when(manager.getTransaction(any())).thenAnswer(ignored -> new SimpleTransactionStatus());
        when(repository.findManifest(initialHash)).thenReturn(initial);
        when(repository.findStoredRelease(initialHash))
                .thenReturn(new StoredRelease.Legacy(initialHash, initial));
        when(repository.current("default")).thenReturn(new Deployment("default", initialHash, 1));
        releases = new ReleaseService(() -> initial, repository, new TransactionTemplate(manager));
    }

    @Test
    void everyReadVerifiesManifestHashAndDoesNotSubstituteCurrentDefinition() {
        when(repository.findManifest(initialHash)).thenReturn(definition("tampered"));
        when(repository.findStoredRelease(initialHash))
                .thenReturn(new StoredRelease.Legacy(initialHash, definition("tampered")));
        assertThatThrownBy(() -> releases.require(initialHash))
                .isInstanceOf(Blocked.class)
                .hasMessage("release_unavailable");
        assertThatThrownBy(() -> releases.require("f".repeat(64)))
                .isInstanceOf(Blocked.class)
                .hasMessage("release_unavailable");
        assertThatThrownBy(releases::currentHash).hasMessage("release_unavailable");
    }

    @Test
    void currentPointerIsReadFromRepositoryOnEachCall() {
        var next = definition("next");
        String hash = ReleaseIdentity.hash(next);
        when(repository.findManifest(hash)).thenReturn(next);
        when(repository.current("default")).thenReturn(new Deployment("default", hash, 2));
        assertThat(releases.currentHash()).isEqualTo(hash);
        assertThat(releases.require(initialHash)).isEqualTo(initial);
    }

    @Test
    void invalidHistoryAndRevisionDoNotReachMutationBoundary() {
        assertThatThrownBy(() -> releases.activate(initialHash, 0))
                .hasMessage("invalid_release_revision");
        assertThatThrownBy(() -> releases.history(-1, 10))
                .hasMessage("invalid_release_history_page");
        assertThatThrownBy(() -> releases.history(0, 101))
                .hasMessage("invalid_release_history_page");
        verify(repository, never()).updateDeployment(anyString(), anyString(), anyLong());
    }

    @Test
    void openingExistingCatalogDoesNotInitializeOrParseStartupConfiguration() {
        var stored = mock(ReleaseRepository.class);
        var manager = mock(PlatformTransactionManager.class);
        var opened = ReleaseService.open(stored, new TransactionTemplate(manager));
        verifyNoInteractions(stored, manager);

        var change = new DeploymentChange("default", 1, initialHash, null);
        when(stored.history("default", 0, 10)).thenReturn(List.of(change));
        assertThat(opened.history(0, 10)).containsExactly(change);
        verify(stored).history("default", 0, 10);
        verifyNoMoreInteractions(stored);
        verifyNoInteractions(manager);
    }

    @Test
    void openingMissingCatalogDoesNotInventPointerOrManifest() {
        var stored = mock(ReleaseRepository.class);
        var opened =
                ReleaseService.open(
                        stored, new TransactionTemplate(mock(PlatformTransactionManager.class)));
        assertThatThrownBy(opened::currentHash)
                .isInstanceOf(Blocked.class)
                .hasMessage("release_unavailable");
        verify(stored).current("default");
        verifyNoMoreInteractions(stored);
    }

    @Test
    void eachResolveReadsTheSelectedAggregateWithoutLegacyFallbackOrCurrentPointer() {
        clearInvocations(repository);
        assertThat(releases.resolve(initialHash).agentId()).isEqualTo(AgentDraft.BUILTIN_AGENT);
        assertThat(releases.require(initialHash)).isEqualTo(initial);
        verify(repository, times(2)).findStoredRelease(initialHash);
        verifyNoMoreInteractions(repository);
    }

    @Test
    void invalidHashOrWrongReturnedIdentityCannotReachAnExecutionDefinition() {
        clearInvocations(repository);
        assertThatThrownBy(() -> releases.resolve(null)).hasMessage("release_unavailable");
        assertThatThrownBy(() -> releases.resolve(" ")).hasMessage("release_unavailable");
        verifyNoInteractions(repository);

        when(repository.findStoredRelease(initialHash))
                .thenReturn(new StoredRelease.Legacy("f".repeat(64), initial));
        assertThatThrownBy(() -> releases.resolve(initialHash)).hasMessage("release_unavailable");
        verify(repository).findStoredRelease(initialHash);
        verifyNoMoreInteractions(repository);
    }

    @ParameterizedTest
    @ValueSource(booleans = {false, true})
    void validAgentAggregateKeepsItsOwnerPromptAndDefinitionProjection(boolean composed) {
        var plan = emptyPlan();
        var stored =
                agent(
                        composed,
                        PRIVATE_AGENT,
                        runtime(composed),
                        new StoredRelease.LegacyBase(initialHash, initial),
                        plan);
        stub(stored);
        clearInvocations(repository);
        var resolved = releases.resolve(stored.hash());
        assertThat(resolved.agentId()).isEqualTo(PRIVATE_AGENT);
        assertThat(resolved.systemPrompt()).isEqualTo("Private prompt");
        assertThat(resolved.tenant()).isEqualTo("tenant");
        assertThat(resolved.subject()).isEqualTo("alice");
        assertThat(resolved.definition()).isEqualTo(composed ? plan.definition() : initial);
        assertThat(resolved.plan()).isEqualTo(composed ? plan : null);
        verify(repository).findStoredRelease(stored.hash());
        verifyNoMoreInteractions(repository);
    }

    @ParameterizedTest
    @ValueSource(booleans = {false, true})
    void outerHashIsCheckedBeforeInvalidManifestRuntimeAndUnavailableBase(boolean composed) {
        var invalid = agent(composed, "invalid-agent", "future-runtime", null, emptyPlan());
        String requested = "f".repeat(64);
        StoredRelease mismatched =
                invalid instanceof StoredRelease.AgentV2 v2
                        ? new StoredRelease.AgentV2(requested, v2.manifest(), null)
                        : new StoredRelease.AgentV3(
                                requested, ((StoredRelease.AgentV3) invalid).manifest(), null);
        when(repository.findStoredRelease(requested)).thenReturn(mismatched);
        assertThatThrownBy(() -> releases.resolve(requested))
                .isInstanceOf(Blocked.class)
                .hasMessage("release_unavailable");
    }

    @ParameterizedTest
    @ValueSource(booleans = {false, true})
    void invalidOuterManifestIsNotHiddenByAnUnavailableJoinedBase(boolean composed) {
        var stored = agent(composed, "invalid-agent", runtime(composed), null, emptyPlan());
        stub(stored);
        assertThatThrownBy(() -> releases.resolve(stored.hash()))
                .isInstanceOf(Blocked.class)
                .hasMessage("agent_manifest_invalid");
    }

    @ParameterizedTest
    @ValueSource(booleans = {false, true})
    void incompatibleOuterRuntimeIsNotHiddenByAnUnavailableJoinedBase(boolean composed) {
        var stored = agent(composed, PRIVATE_AGENT, "future-runtime", null, emptyPlan());
        stub(stored);
        assertThatThrownBy(() -> releases.resolve(stored.hash()))
                .isInstanceOf(Blocked.class)
                .hasMessage("agent_runtime_incompatible");
    }

    @ParameterizedTest
    @ValueSource(booleans = {false, true})
    void validOuterManifestStillRejectsMissingWrongIdentityOrTamperedBase(boolean composed) {
        var missing = agent(composed, PRIVATE_AGENT, runtime(composed), null, emptyPlan());
        stub(missing);
        assertThatThrownBy(() -> releases.resolve(missing.hash()))
                .hasMessage("release_unavailable");

        var wrongIdentity =
                agent(
                        composed,
                        PRIVATE_AGENT,
                        runtime(composed),
                        new StoredRelease.LegacyBase("f".repeat(64), initial),
                        emptyPlan());
        stub(wrongIdentity);
        assertThatThrownBy(() -> releases.resolve(wrongIdentity.hash()))
                .hasMessage("release_unavailable");

        var tampered =
                agent(
                        composed,
                        PRIVATE_AGENT,
                        runtime(composed),
                        new StoredRelease.LegacyBase(initialHash, definition("tampered-base")),
                        emptyPlan());
        stub(tampered);
        assertThatThrownBy(() -> releases.resolve(tampered.hash()))
                .hasMessage("release_unavailable");
    }

    @Test
    void composedPlanIsValidatedAfterTheBaseAndEvenWhenItsOuterHashMatches() {
        // 请求与实际闭包都为空，definition 却带所有 scope；这是摘要合法但投影越权的 v3 计划。
        var expanded =
                new AgentExecutionPlan("primary", "a".repeat(64), List.of(), List.of(), initial);
        var noBase = agent(true, PRIVATE_AGENT, runtime(true), null, expanded);
        stub(noBase);
        assertThatThrownBy(() -> releases.resolve(noBase.hash())).hasMessage("release_unavailable");

        var validBase =
                agent(
                        true,
                        PRIVATE_AGENT,
                        runtime(true),
                        new StoredRelease.LegacyBase(initialHash, initial),
                        expanded);
        stub(validBase);
        assertThatThrownBy(() -> releases.resolve(validBase.hash()))
                .isInstanceOf(Blocked.class)
                .hasMessage("agent_plan_invalid");
    }

    private void stub(StoredRelease stored) {
        when(repository.findStoredRelease(stored.hash())).thenReturn(stored);
    }

    private StoredRelease agent(
            boolean composed,
            String agentId,
            String runtimeIdentity,
            StoredRelease.LegacyBase base,
            AgentExecutionPlan plan) {
        if (composed) {
            var manifest =
                    new ComposedAgentReleaseManifest(
                            ComposedAgentReleaseManifest.FORMAT,
                            agentId,
                            "tenant",
                            "alice",
                            1,
                            "Private Agent",
                            "Private prompt",
                            initialHash,
                            plan,
                            runtimeIdentity);
            return new StoredRelease.AgentV3(
                    ComposedAgentReleaseIdentity.hash(manifest), manifest, base);
        }
        var manifest =
                new AgentReleaseManifest(
                        AgentReleaseManifest.FORMAT,
                        agentId,
                        "tenant",
                        "alice",
                        1,
                        "Private Agent",
                        "Private prompt",
                        initialHash,
                        runtimeIdentity);
        return new StoredRelease.AgentV2(AgentReleaseIdentity.hash(manifest), manifest, base);
    }

    private AgentExecutionPlan emptyPlan() {
        var projection =
                new ReleaseDefinition(
                        initial.version(),
                        initial.agent(),
                        List.of(),
                        initial.primary(),
                        initial.fallback(),
                        initial.maxModelCalls(),
                        initial.maxToolCalls(),
                        initial.business());
        return new AgentExecutionPlan("primary", "a".repeat(64), List.of(), List.of(), projection);
    }

    private static String runtime(boolean composed) {
        return composed
                ? ComposedAgentReleaseManifest.RUNTIME_IDENTITY
                : AgentReleaseManifest.RUNTIME_IDENTITY;
    }

    private static ReleaseDefinition definition(String version) {
        return new ReleaseDefinition(
                version,
                "finance_agent",
                ReleaseDefinition.SCOPES,
                ReleaseDefinition.OFFLINE,
                null,
                12,
                32,
                new BusinessFeatures(List.of(), "test-skills", "{}", null));
    }
}
