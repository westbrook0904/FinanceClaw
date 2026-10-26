package com.financeclaw.architecture;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.Mockito.*;

import com.financeclaw.app.artifact.ArtifactService;
import com.financeclaw.app.artifact.port.ArtifactGateway;
import com.financeclaw.app.execution.ExecutionLeaseService;
import com.financeclaw.app.execution.ExecutionPolicyService;
import com.financeclaw.app.release.ReleaseService;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;
import com.financeclaw.domain.agent.ResolvedAgentRelease;
import com.financeclaw.domain.release.BusinessFeatures;
import com.financeclaw.domain.release.ReleaseDefinition;
import com.financeclaw.support.ArtifactPayload;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.function.Supplier;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.CsvSource;
import org.junit.jupiter.params.provider.ValueSource;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.support.SimpleTransactionStatus;
import org.springframework.transaction.support.TransactionTemplate;

/** Exercise authorization order and fence scope; PostgreSQL tests cover actual locking/rollback. */
class ArtifactServiceTest {
    private final Owner owner = new Owner("tenant", "subject");
    private final Turn turn =
            new Turn(
                    "turn",
                    "conversation",
                    "tenant",
                    "subject",
                    "key",
                    "request",
                    "input",
                    "release",
                    "running",
                    1,
                    null,
                    null);
    private final Claim claim =
            new Claim(new Command("command", "turn", "running", "attempt", 1, true, null), turn);
    private ArtifactGateway gateway;
    private ExecutionLeaseService leases;
    private ExecutionPolicyService policy;
    private ReleaseService releases;
    private PlatformTransactionManager transactions;
    private ArtifactService service;

    @BeforeEach
    void setup() {
        gateway = mock(ArtifactGateway.class);
        leases = mock(ExecutionLeaseService.class);
        policy = mock(ExecutionPolicyService.class);
        releases = mock(ReleaseService.class);
        transactions = mock(PlatformTransactionManager.class);
        when(transactions.getTransaction(any())).thenReturn(new SimpleTransactionStatus());
        service =
                new ArtifactService(
                        gateway, leases, policy, releases, new TransactionTemplate(transactions));
        when(gateway.artifact("artifact")).thenReturn(artifact("tenant", "subject"));
        when(gateway.artifactAccess("artifact"))
                .thenReturn(new ArtifactGateway.Access("ziwei:read", false));
        when(gateway.turn("turn")).thenReturn(turn);
        when(gateway.lockGrant("conversation"))
                .thenReturn(new Grant("conversation", 1, "[\"artifact:read\",\"ziwei:read\"]"));
        when(releases.resolve("release"))
                .thenReturn(resolvedRelease(List.of("artifact:read", "ziwei:read")));
        when(leases.fenced(eq(claim), any()))
                .thenAnswer(invocation -> ((Supplier<?>) invocation.getArgument(1)).get());
    }

    @Test
    void readChecksOwnerThenPrivacyThenGrantThenPinnedReleaseWithinTheTransaction() {
        assertThat(service.read(owner, "artifact", 0, 1).data()).isEqualTo(List.of(1));
        var order = inOrder(transactions, gateway, policy, releases);
        order.verify(transactions).getTransaction(any());
        order.verify(gateway).artifact("artifact");
        order.verify(gateway).artifactAccess("artifact");
        order.verify(gateway).turn("turn");
        order.verify(policy).checkArtifactPrivacy(turn);
        order.verify(gateway).lockGrant("conversation");
        order.verify(gateway).turn("turn");
        order.verify(releases).resolve("release");
        order.verify(transactions).commit(any());
    }

    @ParameterizedTest
    @CsvSource({"other,subject", "tenant,other"})
    void foreignOwnerStopsBeforePrivacyAndGrantLocks(String tenant, String subject) {
        when(gateway.artifact("artifact")).thenReturn(artifact(tenant, subject));
        problem(() -> service.read(owner, "artifact", 0, 1), 404, "not_found");
        verify(gateway, never()).artifactAccess(any());
        verify(gateway, never()).lockGrant(any());
        verifyNoInteractions(policy, releases);
    }

    @Test
    void missingArtifactUsesTheSameNotFoundContractAsForeignOwner() {
        when(gateway.artifact("artifact")).thenReturn(null);
        problem(() -> service.read(owner, "artifact", 0, 1), 404, "not_found");
        verify(gateway, never()).artifactAccess(any());
        verifyNoInteractions(policy, releases);
    }

    @Test
    void forgottenArtifactStopsBeforeTheGrantLock() {
        doThrow(new Problem(409, "privacy_epoch_changed")).when(policy).checkArtifactPrivacy(turn);
        problem(() -> service.read(owner, "artifact", 0, 1), 409, "privacy_epoch_changed");
        verify(gateway, never()).lockGrant(any());
        verifyNoInteractions(releases);
        verify(transactions).rollback(any());
    }

    @Test
    void artifactsWithoutPrivacyScopeDoNotAcquireOwnerPrivacyLocks() {
        when(gateway.artifactAccess("artifact"))
                .thenReturn(new ArtifactGateway.Access("artifact:read", false));
        service.read(owner, "artifact", 0, 1);
        verifyNoInteractions(policy);
        var order = inOrder(gateway, releases);
        order.verify(gateway).artifact("artifact");
        order.verify(gateway).artifactAccess("artifact");
        order.verify(gateway).lockGrant("conversation");
        // Source Turn supplies the Agent identity; it does not acquire an owner privacy lock.
        order.verify(gateway).turn("turn");
        order.verify(releases).resolve("release");
        order.verifyNoMoreInteractions();
    }

    @Test
    void missingOrRevokedReadGrantStopsBeforePinnedReleaseAndPayloadChecks() {
        when(gateway.lockGrant("conversation")).thenReturn(null);
        problem(() -> service.read(owner, "artifact", -1, 0, null), 403, "artifact_denied");
        when(gateway.lockGrant("conversation"))
                .thenReturn(new Grant("conversation", 2, "[\"ziwei:read\"]"));
        problem(() -> service.read(owner, "artifact", -1, 0, null), 403, "artifact_denied");
        verifyNoInteractions(releases);
    }

    @Test
    void revokedRequiredScopeStopsBeforePinnedReleaseAndPayloadChecks() {
        when(gateway.lockGrant("conversation"))
                .thenReturn(new Grant("conversation", 2, "[\"artifact:read\"]"));
        problem(() -> service.read(owner, "artifact", -1, 0, null), 403, "artifact_scope_denied");
        verifyNoInteractions(releases);
    }

    @ParameterizedTest
    @ValueSource(strings = {"release_disabled", "release_unavailable"})
    void unavailablePinnedReleaseKeepsConflictAndPrecedesPayloadValidation(String reason) {
        var original = artifact("tenant", "subject");
        when(gateway.artifact("artifact"))
                .thenReturn(
                        new Artifact(
                                original.id(),
                                original.turnId(),
                                original.conversationId(),
                                original.tenant(),
                                original.subject(),
                                original.releaseHash(),
                                "corrupt",
                                "{",
                                1));
        when(releases.resolve("release")).thenThrow(new Blocked(reason));
        problem(() -> service.read(owner, "artifact", -1, 0, null), 409, reason);
        verify(transactions).rollback(any());
    }

    @Test
    void sourceReleaseIdentityMismatchPrecedesPayloadValidation() {
        var resolved = resolvedRelease(List.of("artifact:read", "ziwei:read"));
        when(releases.resolve("release"))
                .thenReturn(
                        new ResolvedAgentRelease(
                                "finance_agent",
                                "prompt",
                                resolved.definition(),
                                "other-tenant",
                                owner.subject()));
        problem(() -> service.read(owner, "artifact", -1, 0, null), 409, "agent_identity_mismatch");
        verify(transactions).rollback(any());
    }

    @Test
    void scopedArchiveUsesOneFenceAndOnePolicyReadForEntityAndScope() {
        var depth = new AtomicInteger();
        when(leases.fenced(eq(claim), any()))
                .thenAnswer(
                        invocation -> {
                            depth.incrementAndGet();
                            try {
                                return ((Supplier<?>) invocation.getArgument(1)).get();
                            } finally {
                                depth.decrementAndGet();
                            }
                        });
        when(gateway.policy("turn")).thenReturn(new Policy("turn", 1, "[\"ziwei:read\"]", 0, 0));
        when(gateway.insertArtifact(any()))
                .thenAnswer(
                        invocation -> {
                            assertThat(depth.get()).isEqualTo(1);
                            return 1;
                        });
        when(gateway.setArtifactScope(any(), eq("ziwei:read")))
                .thenAnswer(
                        invocation -> {
                            assertThat(depth.get()).isEqualTo(1);
                            return 1;
                        });
        var payload = Map.of("result", "海");
        var artifact = service.archive(claim, payload, "ziwei:read");
        assertThat(artifact.turnId()).isEqualTo(turn.id());
        assertThat(artifact.conversationId()).isEqualTo(turn.conversationId());
        assertThat(artifact.tenant()).isEqualTo(owner.tenant());
        assertThat(artifact.subject()).isEqualTo(owner.subject());
        assertThat(artifact.releaseHash()).isEqualTo(turn.releaseHash());
        assertThat(artifact.hash()).isEqualTo(ArtifactPayload.encode(payload).hash());
        assertThat(artifact.bytes()).isEqualTo(ArtifactPayload.encode(payload).bytes());
        verify(policy).checkPolicy(claim);
        verify(gateway).setArtifactScope(artifact.id(), "ziwei:read");
        assertThat(depth.get()).isZero();
    }

    @Test
    void scopedArchiveRejectsScopeBeforeEncodingOversizedPayloadOrWriting() {
        when(gateway.policy("turn")).thenReturn(new Policy("turn", 1, "[]", 0, 0));
        assertThatThrownBy(() -> service.archive(claim, "x".repeat(2097152), "ziwei:read"))
                .isInstanceOf(Blocked.class)
                .hasMessage("artifact_scope_denied");
        verify(policy).checkPolicy(claim);
        verify(leases).fenced(eq(claim), any());
        verify(gateway, never()).insertArtifact(any());
        verify(gateway, never()).setArtifactScope(any(), any());
    }

    @Test
    void unscopedArchiveRejectsOversizedPayloadBeforeEnteringTheFence() {
        assertThatThrownBy(() -> service.archive(claim, "x".repeat(2097152)))
                .isInstanceOf(Blocked.class)
                .hasMessage("artifact_too_large");
        verifyNoInteractions(leases, policy);
        verify(gateway, never()).insertArtifact(any());
    }

    @Test
    void toolResultArchivePublishesPayloadScopeAndPrivacyInOneWriteUnderTheFence() {
        var depth = new AtomicInteger();
        when(leases.fenced(eq(claim), any()))
                .thenAnswer(
                        invocation -> {
                            depth.incrementAndGet();
                            try {
                                return ((Supplier<?>) invocation.getArgument(1)).get();
                            } finally {
                                depth.decrementAndGet();
                            }
                        });
        when(policy.checkPolicy(claim))
                .thenReturn(new Policy("turn", 1, "[\"catalog:read\"]", 0, 0));
        when(gateway.insertToolResultArtifact(any(), eq("catalog:read")))
                .thenAnswer(
                        invocation -> {
                            assertThat(depth.get()).isEqualTo(1);
                            return 1;
                        });
        var payload = Map.of("output", "海".repeat(10000));
        var artifact = service.archiveToolResult(claim, payload, "catalog:read");
        assertThat(artifact.tenant()).isEqualTo(owner.tenant());
        assertThat(artifact.subject()).isEqualTo(owner.subject());
        assertThat(artifact.turnId()).isEqualTo(turn.id());
        assertThat(artifact.hash()).isEqualTo(ArtifactPayload.encode(payload).hash());
        verify(gateway).insertToolResultArtifact(artifact, "catalog:read");
        verify(gateway, never()).policy(any());
        verify(gateway, never()).insertArtifact(any());
        verify(gateway, never()).setArtifactScope(any(), any());
        verify(policy).checkPolicy(claim);
        assertThat(depth.get()).isZero();
    }

    @Test
    void toolResultArchiveRejectsRevocationAndDoesNotPublishAReferenceOnFailedInsert() {
        when(policy.checkPolicy(claim)).thenReturn(new Policy("turn", 1, "[]", 0, 0));
        assertThatThrownBy(() -> service.archiveToolResult(claim, "body", "catalog:read"))
                .isInstanceOf(Blocked.class)
                .hasMessage("artifact_scope_denied");
        verify(gateway, never()).insertToolResultArtifact(any(), any());
        when(policy.checkPolicy(claim))
                .thenReturn(new Policy("turn", 1, "[\"artifact:read\"]", 0, 0));
        assertThatThrownBy(() -> service.archiveToolResult(claim, "body", null))
                .isInstanceOf(Blocked.class)
                .hasMessage("artifact_archive_failed");
    }

    @ParameterizedTest
    @ValueSource(strings = {"range", "download"})
    void fileReadsKeepOwnerIsolationBeforePrivacyOrGrantChecks(String operation) {
        when(gateway.artifact("artifact")).thenReturn(artifact("tenant", "other"));
        problem(() -> fileRead(operation), 404, "not_found");
        verify(gateway, never()).artifactAccess(any());
        verify(gateway, never()).lockGrant(any());
        verifyNoInteractions(policy, releases);
    }

    @ParameterizedTest
    @ValueSource(strings = {"range", "download"})
    void privacyBoundToolFilesCheckForgetEvenWhenOriginalToolHasNoPrivacyScope(String operation) {
        when(gateway.artifactAccess("artifact"))
                .thenReturn(new ArtifactGateway.Access("artifact:read", true));
        doThrow(new Problem(409, "privacy_epoch_changed")).when(policy).checkArtifactPrivacy(turn);
        problem(() -> fileRead(operation), 409, "privacy_epoch_changed");
        verify(gateway, never()).lockGrant(any());
        verifyNoInteractions(releases);
        verify(transactions).rollback(any());
    }

    @ParameterizedTest
    @ValueSource(strings = {"range", "download"})
    void fileReadsRecheckTheOriginalToolScopeAndPinnedRelease(String operation) {
        when(gateway.lockGrant("conversation"))
                .thenReturn(new Grant("conversation", 2, "[\"artifact:read\"]"));
        problem(() -> fileRead(operation), 403, "artifact_scope_denied");
        verifyNoInteractions(releases);
        when(gateway.lockGrant("conversation"))
                .thenReturn(new Grant("conversation", 2, "[\"artifact:read\",\"ziwei:read\"]"));
        when(releases.resolve("release")).thenThrow(new Blocked("release_disabled"));
        problem(() -> fileRead(operation), 409, "release_disabled");
    }

    @Test
    void fileReadsReturnTheSameStoredBytesAndDigest() {
        var range = service.readRange(owner, "artifact", 0, 4);
        var document = service.download(owner, "artifact");
        assertThat(range.text()).isEqualTo("[1,2");
        assertThat(range.nextStart()).isEqualTo(4);
        assertThat(range.complete()).isFalse();
        assertThat(document.hash()).isEqualTo(range.hash());
        assertThat(new String(document.content(), java.nio.charset.StandardCharsets.UTF_8))
                .isEqualTo("[1,2]");
    }

    @ParameterizedTest
    @ValueSource(strings = {"range", "download"})
    void skillOnlyGrantCanReadItsOffloadedFileButNotOrdinaryArtifacts(String operation) {
        when(gateway.artifactAccess("artifact"))
                .thenReturn(new ArtifactGateway.Access("skill:use", true));
        when(gateway.lockGrant("conversation"))
                .thenReturn(new Grant("conversation", 1, "[\"skill:use\"]"));
        fileRead(operation);
        verify(policy).checkArtifactPrivacy(turn);
        verify(releases).resolve("release");

        when(gateway.artifactAccess("artifact"))
                .thenReturn(new ArtifactGateway.Access("skill:use", false));
        problem(() -> fileRead(operation), 403, "artifact_denied");
    }

    @Test
    void composedHelperCannotBorrowTheSourceConversationsBroaderGrant() {
        executionScopes(List.of(), List.of("ziwei:read", "artifact:read"));
        assertThatThrownBy(
                        () -> service.readRangeForExecution(claim, List.of(), "artifact", 0, 512))
                .isInstanceOf(Blocked.class)
                .hasMessage("artifact_scope_denied");
        // Only the executing plan is resolved; denied scope stops before source-release access.
        verify(releases).resolve("release");
        verifyNoMoreInteractions(releases);
        // 既有 HTTP/owner 读取保持原来的来源会话授权语义。
        assertThat(service.readRange(owner, "artifact", 0, 512).text()).isEqualTo("[1,2]");
    }

    @ParameterizedTest
    @ValueSource(strings = {"plan", "current", "narrow"})
    void executionReadRequiresTheSourceScopeInAllThreeAuthoritativeBoundaries(String missing) {
        var full = List.of("artifact:read", "ziwei:read");
        executionScopes(
                missing.equals("plan") ? List.of() : full,
                missing.equals("current") ? List.of() : full);
        var effective = missing.equals("narrow") ? List.<String>of() : full;
        assertThatThrownBy(() -> service.readForExecution(claim, effective, "artifact", 0, 1, ""))
                .hasMessage("artifact_scope_denied");
        verify(releases).resolve("release");
        verifyNoMoreInteractions(releases);
    }

    @Test
    void executionSkillToolArtifactNeedsItsOriginalScopeButOrdinaryArtifactAlsoNeedsReadScope() {
        executionScopes(List.of("skill:use"), List.of("skill:use"));
        when(gateway.artifactAccess("artifact"))
                .thenReturn(new ArtifactGateway.Access("skill:use", true));
        when(gateway.lockGrant("conversation"))
                .thenReturn(new Grant("conversation", 1, "[\"skill:use\"]"));
        assertThat(
                        service.readRangeForExecution(
                                        claim, List.of("skill:use"), "artifact", 0, 512)
                                .text())
                .isEqualTo("[1,2]");
        when(gateway.artifactAccess("artifact"))
                .thenReturn(new ArtifactGateway.Access("skill:use", false));
        assertThatThrownBy(
                        () ->
                                service.readRangeForExecution(
                                        claim, List.of("skill:use"), "artifact", 0, 512))
                .hasMessage("artifact_scope_denied");
    }

    @ParameterizedTest
    @ValueSource(strings = {"a-source", "z-source"})
    void executionReadsLockBothConversationGrantsInStableOrderAfterOwnerPrivacy(String source) {
        var full = List.of("artifact:read", "ziwei:read");
        executionScopes(full, full);
        var original = artifact("tenant", "subject");
        when(gateway.artifact("artifact"))
                .thenReturn(
                        new Artifact(
                                original.id(),
                                original.turnId(),
                                source,
                                original.tenant(),
                                original.subject(),
                                original.releaseHash(),
                                original.hash(),
                                original.payload(),
                                original.bytes()));
        when(gateway.lockGrant(source))
                .thenReturn(new Grant(source, 1, "[\"artifact:read\",\"ziwei:read\"]"));
        service.readRangeForExecution(claim, full, "artifact", 0, 512);
        var order = inOrder(policy, gateway, releases);
        order.verify(policy, times(2)).checkArtifactPrivacy(turn);
        for (String conversation : List.of("conversation", source).stream().sorted().toList()) {
            order.verify(gateway).lockGrant(conversation);
        }
        order.verify(policy).checkPolicy(claim);
        order.verify(releases).resolve("release");
        order.verify(gateway).turn("turn");
        order.verify(releases).resolve("release");
        order.verifyNoMoreInteractions();
        verify(leases).fenced(eq(claim), any());
    }

    @Test
    void executionReadKeepsFenceAndRevocationChecksBeforeReturningPayload() {
        executionScopes(
                List.of("artifact:read", "ziwei:read"), List.of("artifact:read", "ziwei:read"));
        doThrow(new Blocked("authorization_changed")).when(policy).checkPolicy(claim);
        assertThatThrownBy(
                        () ->
                                service.readRangeForExecution(
                                        claim,
                                        List.of("artifact:read", "ziwei:read"),
                                        "artifact",
                                        0,
                                        512))
                .hasMessage("authorization_changed");
        verify(releases, never()).resolve(any());
        verify(releases, never()).require(any());
    }

    private void executionScopes(List<String> fixed, List<String> current) {
        when(releases.resolve("release")).thenReturn(resolvedRelease(fixed));
        when(policy.checkPolicy(claim))
                .thenReturn(
                        new Policy(
                                "turn",
                                1,
                                com.financeclaw.support.PayloadJson.canonical(current),
                                0,
                                0));
    }

    private ResolvedAgentRelease resolvedRelease(List<String> fixed) {
        var definition =
                new ReleaseDefinition(
                        "v1",
                        "finance_agent",
                        fixed,
                        ReleaseDefinition.OFFLINE,
                        null,
                        12,
                        32,
                        new BusinessFeatures(List.of(), "skills", "{}", null));
        return new ResolvedAgentRelease("finance_agent", "prompt", definition, null, null);
    }

    private void fileRead(String operation) {
        if ("range".equals(operation)) {
            service.readRange(owner, "artifact", 0, 4096);
        } else {
            service.download(owner, "artifact");
        }
    }

    private Artifact artifact(String tenant, String subject) {
        var encoded = ArtifactPayload.encode(List.of(1, 2));
        return new Artifact(
                "artifact",
                "turn",
                "conversation",
                tenant,
                subject,
                "release",
                encoded.hash(),
                encoded.json(),
                encoded.bytes());
    }

    private static void problem(Runnable action, int status, String code) {
        assertThatThrownBy(action::run)
                .isInstanceOfSatisfying(
                        Problem.class, error -> assertThat(error.status()).isEqualTo(status))
                .hasMessage(code);
    }
}
