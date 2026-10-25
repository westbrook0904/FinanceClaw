package com.financeclaw.app.execution;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

import com.financeclaw.app.execution.port.CommandGateway;
import com.financeclaw.app.task.TaskTransitionService;
import com.financeclaw.app.task.port.TaskMutationGateway;
import com.financeclaw.domain.Task.*;
import java.util.concurrent.atomic.AtomicReference;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.support.SimpleTransactionStatus;
import org.springframework.transaction.support.TransactionTemplate;

/** 领取返回值和失败边界的单元契约；锁竞争、真实 RETURNING 及 SQL 数量另由 WorkerClaimQueryIT 验证。 */
class ExecutionLeaseClaimTest {
    private static final Command QUEUED =
            new Command("command", "turn", "queued", null, 7, false, "resume");
    private static final Turn QUEUED_TURN = turn("queued", 1);
    private static final Turn RUNNING_TURN = turn("running", 2);
    private final CommandGateway commands = mock(CommandGateway.class);
    private final TaskMutationGateway turns = mock(TaskMutationGateway.class);
    private final TaskTransitionService transitions = mock(TaskTransitionService.class);
    private final PlatformTransactionManager manager = mock(PlatformTransactionManager.class);
    private ExecutionLeaseService leases;

    @BeforeEach
    void setup() {
        when(manager.getTransaction(any())).thenReturn(new SimpleTransactionStatus());
        leases =
                new ExecutionLeaseService(
                        commands, turns, transitions, new TransactionTemplate(manager));
    }

    @Test
    void claimUsesTheReturnedCommandAndKeepsTheOriginalCommandThenTurnLockOrder() {
        when(commands.nextCommand()).thenReturn(QUEUED);
        when(turns.lockTurn("turn")).thenReturn(QUEUED_TURN);
        when(transitions.changeAndReturn(QUEUED_TURN, "running", null, null))
                .thenReturn(RUNNING_TURN);
        var returned = new AtomicReference<Command>();
        when(commands.claim(eq("command"), anyString(), eq("worker"), eq(15000L)))
                .thenAnswer(
                        call -> {
                            var actual =
                                    new Command(
                                            "command",
                                            "turn",
                                            "running",
                                            call.getArgument(1),
                                            8,
                                            true,
                                            "resume");
                            returned.set(actual);
                            return actual;
                        });

        var result = leases.claim("worker", 15000);

        assertThat(result.command()).isSameAs(returned.get());
        assertThat(result.turn()).isSameAs(RUNNING_TURN);
        assertThat(result.command().attemptId()).isNotBlank();
        assertThat(result.command().fence()).isEqualTo(8);
        assertThat(result.command().live()).isTrue();
        assertThat(result.command().resumeId()).isEqualTo("resume");
        var order = inOrder(commands, turns, transitions);
        order.verify(commands).nextCommand();
        order.verify(turns).lockTurn("turn");
        order.verify(commands).claim("command", returned.get().attemptId(), "worker", 15000);
        order.verify(commands).insertAttempt(returned.get());
        order.verify(transitions).changeAndReturn(QUEUED_TURN, "running", null, null);
        order.verifyNoMoreInteractions();
        verify(commands, never()).lockCommand(anyString());
        verify(manager).commit(any());
        verify(manager, never()).rollback(any());
    }

    @Test
    void missingReturningRowRollsBackBeforeCreatingAttemptOrChangingTurn() {
        when(commands.nextCommand()).thenReturn(QUEUED);
        when(turns.lockTurn("turn")).thenReturn(QUEUED_TURN);
        when(commands.claim(eq("command"), anyString(), eq("worker"), eq(15000L))).thenReturn(null);

        assertThatThrownBy(() -> leases.claim("worker", 15000)).isInstanceOf(Fenced.class);

        verify(commands).nextCommand();
        verify(turns).lockTurn("turn");
        verify(commands).claim(eq("command"), anyString(), eq("worker"), eq(15000L));
        verify(commands, never()).insertAttempt(any());
        verify(commands, never()).lockCommand(anyString());
        verifyNoInteractions(transitions);
        verify(manager).rollback(any());
        verify(manager, never()).commit(any());
    }

    @Test
    void emptyQueueDoesNotLockTurnMutateCommandOrCreateAttempt() {
        assertThat(leases.claim("worker", 15000)).isNull();

        verify(commands).nextCommand();
        verifyNoMoreInteractions(commands);
        verifyNoInteractions(turns, transitions);
        verify(manager).commit(any());
        verify(manager, never()).rollback(any());
    }

    private static Turn turn(String status, long revision) {
        return new Turn(
                "turn",
                "conversation",
                "tenant",
                "alice",
                "key",
                "hash",
                "message",
                "release",
                status,
                revision,
                null,
                null);
    }
}
