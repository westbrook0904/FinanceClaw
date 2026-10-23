package com.financeclaw.architecture;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;

import com.financeclaw.app.execution.ExecutionLeaseService;
import com.financeclaw.app.execution.ExecutionPolicyService;
import com.financeclaw.app.execution.port.CommandGateway;
import com.financeclaw.app.report.ReportService;
import com.financeclaw.app.report.port.ReportGateway;
import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.Task.Claim;
import com.financeclaw.domain.Task.Command;
import com.financeclaw.domain.Task.Fenced;
import com.financeclaw.domain.Task.Turn;
import com.financeclaw.domain.report.ReportReceipt;
import com.financeclaw.support.PayloadJson;
import java.util.UUID;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
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
                new ExecutionLeaseService(
                        commands, null, null, null, new TransactionTemplate(manager));
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
        order.verify(db).receipt("turn", "call");
        order.verify(db)
                .publishReport(id, "turn", "call", "Quarterly review", "action-hash", result);
        order.verify(manager).commit(status);
        order.verifyNoMoreInteractions();
    }

    @Test
    void completedEffectReplaysTheOriginalBytesWithoutAnotherReport() {
        String originalResult = "{ \"status\" : \"created\", \"report_id\" : \"first-report\" }";
        when(db.receipt("turn", "call"))
                .thenReturn(new ReportReceipt("action-hash", originalResult));

        assertEquals(
                originalResult,
                reports.report(CLAIM, "call", "action-hash", "Changed display title"));

        var order = inOrder(manager, commands, policy, db);
        order.verify(manager).getTransaction(any());
        order.verify(commands).lockCommand("command");
        order.verify(policy).checkPolicy(CLAIM);
        order.verify(db).receipt("turn", "call");
        order.verify(manager).commit(status);
        order.verifyNoMoreInteractions();
    }

    @Test
    void changedActionHashConflictsWithoutAnotherPublication() {
        when(db.receipt("turn", "call"))
                .thenReturn(new ReportReceipt("older-action-hash", "old-result"));

        Blocked blocked =
                assertThrows(
                        Blocked.class,
                        () -> reports.report(CLAIM, "call", "action-hash", "Quarterly review"));

        assertEquals("effect_conflict", blocked.getMessage());
        verify(db).receipt("turn", "call");
        verifyNoMoreInteractions(db);
        verify(manager).rollback(status);
        verify(manager, never()).commit(any());
    }

    @Test
    void expiredLeaseCannotReadOrWriteTheEffectReceipt() {
        when(commands.lockCommand("command"))
                .thenReturn(new Command("command", "turn", "running", "attempt", 3, false, null));

        assertThrows(
                Fenced.class,
                () -> reports.report(CLAIM, "call", "action-hash", "Quarterly review"));

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
                        Blocked.class,
                        () -> reports.report(CLAIM, "call", "action-hash", "Quarterly review")));

        verifyNoInteractions(db);
        verify(manager).rollback(status);
    }

    @Test
    void publicationFailureRollsBackWithoutSeparateEffectWrites() {
        var failure = new IllegalStateException("report storage unavailable");
        doThrow(failure)
                .when(db)
                .publishReport(
                        anyString(), eq("turn"), eq("call"), anyString(), anyString(), anyString());

        assertSame(
                failure,
                assertThrows(
                        IllegalStateException.class,
                        () -> reports.report(CLAIM, "call", "action-hash", "Quarterly review")));

        var order = inOrder(db, manager);
        order.verify(manager).getTransaction(any());
        order.verify(db).receipt("turn", "call");
        order.verify(db)
                .publishReport(
                        anyString(),
                        eq("turn"),
                        eq("call"),
                        eq("Quarterly review"),
                        eq("action-hash"),
                        anyString());
        order.verify(manager).rollback(status);
        order.verifyNoMoreInteractions();
    }
}
