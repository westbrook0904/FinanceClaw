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
import com.financeclaw.support.ArtifactPayload;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.function.Supplier;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.CsvSource;
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
    when(gateway.artifactScope("artifact")).thenReturn("ziwei:read");
    when(gateway.turn("turn")).thenReturn(turn);
    when(gateway.lockGrant("conversation"))
        .thenReturn(new Grant("conversation", 1, "[\"artifact:read\",\"ziwei:read\"]"));
    when(leases.fenced(eq(claim), any()))
        .thenAnswer(invocation -> ((Supplier<?>) invocation.getArgument(1)).get());
  }

  @Test
  void readChecksOwnerThenPrivacyThenGrantThenPinnedReleaseWithinTheTransaction() {
    assertThat(service.read(owner, "artifact", 0, 1).data()).isEqualTo(List.of(1));
    var order = inOrder(transactions, gateway, policy, releases);
    order.verify(transactions).getTransaction(any());
    order.verify(gateway).artifact("artifact");
    order.verify(gateway).artifactScope("artifact");
    order.verify(gateway).turn("turn");
    order.verify(policy).checkArtifactPrivacy(turn);
    order.verify(gateway).lockGrant("conversation");
    order.verify(gateway).artifactScope("artifact");
    order.verify(releases).require("release");
    order.verify(transactions).commit(any());
  }

  @ParameterizedTest
  @CsvSource({"other,subject", "tenant,other"})
  void foreignOwnerStopsBeforePrivacyAndGrantLocks(String tenant, String subject) {
    when(gateway.artifact("artifact")).thenReturn(artifact(tenant, subject));
    problem(() -> service.read(owner, "artifact", 0, 1), 404, "not_found");
    verify(gateway, never()).artifactScope(any());
    verify(gateway, never()).lockGrant(any());
    verifyNoInteractions(policy, releases);
  }

  @Test
  void missingArtifactUsesTheSameNotFoundContractAsForeignOwner() {
    when(gateway.artifact("artifact")).thenReturn(null);
    problem(() -> service.read(owner, "artifact", 0, 1), 404, "not_found");
    verify(gateway, never()).artifactScope(any());
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
    when(gateway.artifactScope("artifact")).thenReturn("artifact:read");
    service.read(owner, "artifact", 0, 1);
    verify(gateway, never()).turn(any());
    verifyNoInteractions(policy);
    verify(gateway).lockGrant("conversation");
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

  @Test
  void unavailablePinnedReleaseKeepsConflictAndPrecedesPayloadValidation() {
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
    when(releases.require("release")).thenThrow(new Blocked("release_disabled"));
    problem(() -> service.read(owner, "artifact", -1, 0, null), 409, "release_disabled");
  }

  @Test
  void scopedArchiveKeepsNestedFenceAndScopeWriteInsideTheOuterFence() {
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
              assertThat(depth.get()).isEqualTo(2);
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
    verify(policy, times(2)).checkPolicy(claim);
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
