package com.financeclaw.architecture;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;

import com.financeclaw.app.execution.ExecutionLeaseService;
import com.financeclaw.app.execution.ExecutionPolicyService;
import com.financeclaw.app.execution.port.CommandGateway;
import com.financeclaw.app.report.ReportService;
import com.financeclaw.app.report.port.ReportGateway;
import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.Governance.Effect;
import com.financeclaw.domain.Task.Claim;
import com.financeclaw.domain.Task.Command;
import com.financeclaw.domain.Task.Fenced;
import com.financeclaw.domain.Task.Turn;
import com.financeclaw.support.PayloadJson;
import java.util.UUID;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.support.SimpleTransactionStatus;
import org.springframework.transaction.support.TransactionTemplate;

class ReportServiceTest {
  private static final Command COMMAND =
      new Command("command", "turn", "running", "attempt", 3, true, null);
  private static final Claim CLAIM =
      new Claim(
          COMMAND,
          new Turn(
              "turn",
              "conversation",
              "tenant",
              "subject",
              "request",
              "request-hash",
              "create report",
              "release",
              "running",
              2,
              null,
              null));
  private final ReportGateway db = mock(ReportGateway.class);
  private final CommandGateway commands = mock(CommandGateway.class);
  private final ExecutionPolicyService policy = mock(ExecutionPolicyService.class);
  private final PlatformTransactionManager manager = mock(PlatformTransactionManager.class);
  private final SimpleTransactionStatus status = new SimpleTransactionStatus();
  private ReportService reports;

  @BeforeEach
  void setup() {
    when(manager.getTransaction(any())).thenReturn(status);
    when(commands.lockCommand("command")).thenReturn(COMMAND);
    var leases =
        new ExecutionLeaseService(commands, null, null, null, new TransactionTemplate(manager));
    reports = new ReportService(db, leases, policy);
  }

  @Test
  void reportAndReceiptShareTheFencedTransactionAfterAuthorization() {
    String result = reports.report(CLAIM, "call", "action-hash", "Quarterly review");
    var value = PayloadJson.object(result);
    String id = (String) value.get("report_id");
    assertEquals(id, UUID.fromString(id).toString());
    assertEquals("{\"report_id\":\"" + id + "\",\"status\":\"created\"}", result);

    var order = inOrder(manager, commands, policy, db);
    order.verify(manager).getTransaction(any());
    order.verify(commands).lockCommand("command");
    order.verify(policy).checkPolicy(CLAIM);
    order.verify(db).effect("turn", "call");
    order.verify(db).beginEffect("turn", "call", "action-hash");
    order.verify(db).report(id, "turn", "call", "Quarterly review");
    order.verify(db).completeEffect("turn", "call", result);
    order.verify(manager).commit(status);
    order.verifyNoMoreInteractions();
  }

  @Test
  void completedEffectReplaysTheOriginalBytesWithoutAnotherReport() {
    String legacyResult = "{ \"status\" : \"created\", \"report_id\" : \"legacy-report\" }";
    when(db.effect("turn", "call"))
        .thenReturn(new Effect("action-hash", "completed", legacyResult));

    assertEquals(
        legacyResult, reports.report(CLAIM, "call", "action-hash", "Changed display title"));

    var order = inOrder(manager, commands, policy, db);
    order.verify(manager).getTransaction(any());
    order.verify(commands).lockCommand("command");
    order.verify(policy).checkPolicy(CLAIM);
    order.verify(db).effect("turn", "call");
    order.verify(manager).commit(status);
    order.verifyNoMoreInteractions();
  }

  @ParameterizedTest
  @ValueSource(strings = {"completed", "pending", "unknown"})
  void changedActionHashConflictsBeforeConsideringEffectStatus(String effectStatus) {
    when(db.effect("turn", "call"))
        .thenReturn(new Effect("older-action-hash", effectStatus, "old-result"));

    Blocked blocked =
        assertThrows(
            Blocked.class, () -> reports.report(CLAIM, "call", "action-hash", "Quarterly review"));

    assertEquals("effect_conflict", blocked.getMessage());
    verify(db).effect("turn", "call");
    verifyNoMoreInteractions(db);
    verify(manager).rollback(status);
    verify(manager, never()).commit(any());
  }

  @ParameterizedTest
  @ValueSource(strings = {"pending", "unknown", "failed"})
  void unresolvedEffectRequiresReconciliationWithoutRetryingTheWrite(String effectStatus) {
    when(db.effect("turn", "call"))
        .thenReturn(new Effect("action-hash", effectStatus, "old-result"));

    Blocked blocked =
        assertThrows(
            Blocked.class, () -> reports.report(CLAIM, "call", "action-hash", "Quarterly review"));

    assertEquals("reconciliation_required", blocked.getMessage());
    verify(db).effect("turn", "call");
    verifyNoMoreInteractions(db);
    verify(manager).rollback(status);
  }

  @Test
  void expiredLeaseCannotReadOrWriteTheEffectReceipt() {
    when(commands.lockCommand("command"))
        .thenReturn(new Command("command", "turn", "running", "attempt", 3, false, null));

    assertThrows(
        Fenced.class, () -> reports.report(CLAIM, "call", "action-hash", "Quarterly review"));

    verifyNoInteractions(policy, db);
    verify(manager).rollback(status);
  }

  @Test
  void revokedAuthorizationCannotReplayACompletedEffect() {
    var blocked = new Blocked("authorization_changed");
    doThrow(blocked).when(policy).checkPolicy(CLAIM);

    assertSame(
        blocked,
        assertThrows(
            Blocked.class, () -> reports.report(CLAIM, "call", "action-hash", "Quarterly review")));

    verifyNoInteractions(db);
    verify(manager).rollback(status);
  }

  @Test
  void reportFailureRollsBackThePendingEffect() {
    var failure = new IllegalStateException("report storage unavailable");
    doThrow(failure).when(db).report(anyString(), eq("turn"), eq("call"), anyString());

    assertSame(
        failure,
        assertThrows(
            IllegalStateException.class,
            () -> reports.report(CLAIM, "call", "action-hash", "Quarterly review")));

    var order = inOrder(db, manager);
    order.verify(manager).getTransaction(any());
    order.verify(db).effect("turn", "call");
    order.verify(db).beginEffect("turn", "call", "action-hash");
    order.verify(db).report(anyString(), eq("turn"), eq("call"), eq("Quarterly review"));
    order.verify(manager).rollback(status);
    order.verifyNoMoreInteractions();
  }

  @Test
  void receiptFailureRollsBackTheReportAndEffectTogether() {
    var failure = new IllegalStateException("effect storage unavailable");
    doThrow(failure).when(db).completeEffect(eq("turn"), eq("call"), anyString());

    assertSame(
        failure,
        assertThrows(
            IllegalStateException.class,
            () -> reports.report(CLAIM, "call", "action-hash", "Quarterly review")));

    var order = inOrder(db, manager);
    order.verify(manager).getTransaction(any());
    order.verify(db).effect("turn", "call");
    order.verify(db).beginEffect("turn", "call", "action-hash");
    order.verify(db).report(anyString(), eq("turn"), eq("call"), eq("Quarterly review"));
    order.verify(db).completeEffect(eq("turn"), eq("call"), anyString());
    order.verify(manager).rollback(status);
    order.verifyNoMoreInteractions();
  }
}
