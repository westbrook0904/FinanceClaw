package com.financeclaw.architecture;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;

import com.financeclaw.app.governance.GrantService;
import com.financeclaw.app.governance.port.GrantGateway;
import com.financeclaw.app.task.TaskQueryService;
import com.financeclaw.domain.Governance.Grant;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.Task.Problem;
import com.financeclaw.domain.governance.GrantReceipt;
import java.util.Arrays;
import java.util.List;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.support.SimpleTransactionStatus;
import org.springframework.transaction.support.TransactionTemplate;

class GrantServiceTest {
  private static final Owner OWNER = new Owner("tenant", "subject");
  private static final String SCOPES = "[\"artifact:read\",\"report:write\"]";
  // Persisted request format: length-prefixed SHA-256 of revision then sorted canonical scopes.
  private static final String REQUEST_HASH =
      "44b3dcccbda8811fd8fcacde84a8c9b7c3e0a17d7da5d56bdf3c8d57e3e2e848";
  private final TaskQueryService queries = mock(TaskQueryService.class);
  private final GrantGateway db = mock(GrantGateway.class);
  private final PlatformTransactionManager manager = mock(PlatformTransactionManager.class);
  private final SimpleTransactionStatus status = new SimpleTransactionStatus();
  private GrantService grants;

  @BeforeEach
  void setup() {
    when(manager.getTransaction(any())).thenReturn(status);
    grants =
        new GrantService(
            queries,
            db,
            new TransactionTemplate(manager),
            new com.financeclaw.app.task.TaskEventHub());
  }

  @Test
  void duplicateAndReorderedScopesKeepThePersistedRequestHashAndSingleTransaction() {
    when(db.lockGrant("conversation")).thenReturn(new Grant("conversation", 7, "[]"));
    Grant updated = new Grant("conversation", 8, SCOPES);
    when(db.grant("conversation")).thenReturn(updated);

    assertSame(
        updated,
        grants.changeGrant(
            OWNER,
            "conversation",
            "request",
            7,
            List.of("report:write", "artifact:read", "report:write")));

    var order = inOrder(queries, manager, db);
    order.verify(queries).owned(OWNER, "conversation");
    order.verify(manager).getTransaction(any());
    order.verify(db).lockGrant("conversation");
    order.verify(db).grantReceipt("conversation", "request");
    order.verify(db).updateGrant("conversation", SCOPES);
    order.verify(db).grantChange("conversation", "request", REQUEST_HASH);
    order.verify(db).grant("conversation");
    order.verify(manager).commit(status);
    order.verifyNoMoreInteractions();
  }

  @Test
  void replayReturnsTheOriginalReceiptAfterAnotherGrantChange() {
    when(db.lockGrant("conversation")).thenReturn(new Grant("conversation", 15, "[]"));
    Grant original = new Grant("conversation", 8, "[\"report:write\", \"artifact:read\"]");
    when(db.grantReceipt("conversation", "request"))
        .thenReturn(
            new GrantReceipt(
                REQUEST_HASH, original.conversationId(), original.revision(), original.scopes()));

    assertEquals(
        original,
        grants.changeGrant(
            OWNER, "conversation", "request", 7, List.of("artifact:read", "report:write")));

    var order = inOrder(manager, db);
    order.verify(manager).getTransaction(any());
    order.verify(db).lockGrant("conversation");
    order.verify(db).grantReceipt("conversation", "request");
    order.verify(manager).commit(status);
    order.verifyNoMoreInteractions();
  }

  @Test
  void replayConflictWinsOverTheCurrentRevisionAndDoesNotMutateAuthorization() {
    when(db.grantReceipt("conversation", "request"))
        .thenReturn(new GrantReceipt(REQUEST_HASH, "conversation", 8, SCOPES));
    when(db.lockGrant("conversation")).thenReturn(new Grant("conversation", 15, "[]"));

    Problem problem =
        assertThrows(
            Problem.class,
            () ->
                grants.changeGrant(OWNER, "conversation", "request", 7, List.of("artifact:read")));

    assertEquals(409, problem.status());
    assertEquals("idempotency_conflict", problem.getMessage());
    verify(db, never()).updateGrant(any(), any());
    verify(db, never()).grantChange(any(), any(), any());
    verify(manager).rollback(status);
    verify(manager, never()).commit(any());
  }

  @ParameterizedTest
  @ValueSource(longs = {-1, 6, 8})
  void absentOrStaleGrantCannotCreateANewReceipt(long currentRevision) {
    if (currentRevision >= 0)
      when(db.lockGrant("conversation"))
          .thenReturn(new Grant("conversation", currentRevision, "[]"));

    Problem problem =
        assertThrows(
            Problem.class,
            () ->
                grants.changeGrant(OWNER, "conversation", "request", 7, List.of("artifact:read")));

    assertEquals(409, problem.status());
    assertEquals("stale_grant", problem.getMessage());
    verify(db, never()).updateGrant(any(), any());
    verify(db, never()).grantChange(any(), any(), any());
    verify(manager).rollback(status);
  }

  @Test
  void receiptFailureRollsBackTheGrantMutation() {
    when(db.lockGrant("conversation")).thenReturn(new Grant("conversation", 7, "[]"));
    var failure = new IllegalStateException("receipt storage unavailable");
    doThrow(failure).when(db).grantChange("conversation", "request", REQUEST_HASH);

    assertSame(
        failure,
        assertThrows(
            IllegalStateException.class,
            () ->
                grants.changeGrant(
                    OWNER,
                    "conversation",
                    "request",
                    7,
                    List.of("report:write", "artifact:read"))));

    var order = inOrder(manager, db);
    order.verify(manager).getTransaction(any());
    order.verify(db).lockGrant("conversation");
    order.verify(db).grantReceipt("conversation", "request");
    order.verify(db).updateGrant("conversation", SCOPES);
    order.verify(db).grantChange("conversation", "request", REQUEST_HASH);
    order.verify(manager).rollback(status);
    order.verifyNoMoreInteractions();
  }

  @Test
  void missingOwnershipWinsOverInvalidScopesBeforeOpeningATransaction() {
    var missing = new Problem(404, "not_found");
    doThrow(missing).when(queries).owned(OWNER, "conversation");

    assertSame(
        missing,
        assertThrows(
            Problem.class, () -> grants.changeGrant(OWNER, "conversation", "request", 7, null)));

    verifyNoInteractions(db, manager);
  }

  @Test
  void invalidKeyKeepsItsExistingPrecedenceBeforeOwnership() {
    Problem problem =
        assertThrows(Problem.class, () -> grants.changeGrant(OWNER, "conversation", " ", 7, null));

    assertEquals(422, problem.status());
    assertEquals("invalid_idempotency_key", problem.getMessage());
    verifyNoInteractions(queries, db, manager);
  }

  @Test
  void invalidScopesCannotReachPersistence() {
    for (List<String> scopes : Arrays.<List<String>>asList(null, List.of("unknown:write"))) {
      Problem problem =
          assertThrows(
              Problem.class, () -> grants.changeGrant(OWNER, "conversation", "request", 7, scopes));
      assertEquals(422, problem.status());
      assertEquals("invalid_scopes", problem.getMessage());
    }
    verify(queries, times(2)).owned(OWNER, "conversation");
    verifyNoInteractions(db, manager);
  }

  @Test
  void grantReadRequiresOwnershipBeforeReturningPersistedScopes() {
    Grant current = new Grant("conversation", 8, SCOPES);
    when(db.grant("conversation")).thenReturn(current);

    assertSame(current, grants.grant(OWNER, "conversation"));

    var order = inOrder(queries, db);
    order.verify(queries).owned(OWNER, "conversation");
    order.verify(db).grant("conversation");
    verifyNoInteractions(manager);
  }
}
