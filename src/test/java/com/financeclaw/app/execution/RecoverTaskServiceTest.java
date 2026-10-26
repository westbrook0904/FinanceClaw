package com.financeclaw.app.execution;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.Mockito.*;

import com.financeclaw.app.execution.port.CommandGateway;
import com.financeclaw.app.execution.port.ExecutionReceiptGateway;
import com.financeclaw.app.task.TaskTransitionService;
import com.financeclaw.app.task.port.TaskMutationGateway;
import com.financeclaw.domain.Task.Command;
import com.financeclaw.domain.Task.Receipt;
import com.financeclaw.domain.Task.Turn;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.stream.Stream;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.MethodSource;
import org.springframework.transaction.TransactionDefinition;
import org.springframework.transaction.support.AbstractPlatformTransactionManager;
import org.springframework.transaction.support.DefaultTransactionStatus;
import org.springframework.transaction.support.TransactionSynchronizationManager;
import org.springframework.transaction.support.TransactionTemplate;

/** 只验证 Spring 事务包围、预算停止和失败传播；真实 PG 行锁/持久原子性由 CommandRecoveryBatchIT 验证。 */
class RecoverTaskServiceTest {
    private final CommandGateway commands = mock(CommandGateway.class);
    private final TaskMutationGateway turns = mock(TaskMutationGateway.class);
    private final ExecutionReceiptGateway receipts = mock(ExecutionReceiptGateway.class);
    private final ExecutionCompletionService completion = mock(ExecutionCompletionService.class);
    private final TaskTransitionService transitions = mock(TaskTransitionService.class);
    private final LocalTransactions manager = new LocalTransactions();
    private final RecoverTaskService recovery =
            new RecoverTaskService(
                    commands,
                    turns,
                    receipts,
                    completion,
                    transitions,
                    new TransactionTemplate(manager));

    @Test
    void anEmptyLockedQueryStopsInsideOneCommittedTransactionWithoutReadingOtherAuthorities() {
        doAnswer(
                        call -> {
                            assertActive();
                            return null;
                        })
                .when(commands)
                .nextExpiredCommand();
        assertThat(recovery.recoverExpired()).isZero();
        verify(commands).nextExpiredCommand();
        verifyNoMoreInteractions(commands);
        verifyNoInteractions(turns, receipts, completion, transitions);
        assertThat(manager.begins).isOne();
        assertThat(manager.commits).isOne();
        assertThat(manager.rollbacks).isZero();
    }

    @ParameterizedTest
    @MethodSource("unusableCommands")
    void aReturnedLiveOrNonRunningCommandCannotReachTurnOrReceipt(Command unusable) {
        when(commands.nextExpiredCommand()).thenReturn(unusable).thenReturn(null);
        assertThat(recovery.recoverExpired()).isZero();
        verify(commands, times(2)).nextExpiredCommand();
        verifyNoMoreInteractions(commands);
        verifyNoInteractions(turns, receipts, completion, transitions);
        assertThat(manager.begins).isEqualTo(2);
        assertThat(manager.commits).isEqualTo(2);
        assertThat(manager.rollbacks).isZero();
    }

    static Stream<Command> unusableCommands() {
        return Stream.of(
                new Command("command", "turn", "running", "attempt", 1, true, null),
                new Command("command", "turn", "done", "attempt", 1, false, null));
    }

    @Test
    void fiftyReturnedRowsAreTheHardTransactionBudgetWithoutAStaleFiftyFirstProbe() {
        var ordinal = new AtomicInteger();
        doAnswer(
                        call -> {
                            assertActive();
                            int i = ordinal.incrementAndGet();
                            return command(i);
                        })
                .when(commands)
                .nextExpiredCommand();
        doAnswer(
                        call -> {
                            assertActive();
                            return turn();
                        })
                .when(turns)
                .lockTurn(anyString());
        assertThat(recovery.recoverExpired()).isEqualTo(50);
        verify(commands, times(50)).nextExpiredCommand();
        verify(commands, times(50)).finishCommand(anyString());
        verify(turns, times(50)).lockTurn(anyString());
        verify(receipts, times(50)).receipt(anyString());
        verify(transitions, times(50))
                .change(any(Turn.class), eq("blocked"), isNull(), eq("unconfirmed_execution"));
        verifyNoInteractions(completion);
        assertThat(ordinal.get()).isEqualTo(50);
        assertThat(manager.begins).isEqualTo(50);
        assertThat(manager.commits).isEqualTo(50);
        assertThat(manager.rollbacks).isZero();
    }

    @Test
    void aSecondReceiptFailureStopsTheBatchAndRollsBackOnlyThatTransaction() {
        var first = command(1);
        var second = command(2);
        var firstReceipt = new Receipt("attempt-1", "command-1", 1, 3L, "completed", "answer");
        var secondReceipt = new Receipt("attempt-2", "command-2", 1, 3L, "completed", "answer");
        when(commands.nextExpiredCommand())
                .thenReturn(first)
                .thenReturn(second)
                .thenThrow(new AssertionError("No third transaction"));
        doAnswer(
                        call -> {
                            assertActive();
                            return turn();
                        })
                .when(turns)
                .lockTurn("turn");
        when(receipts.receipt("attempt-1")).thenReturn(firstReceipt);
        when(receipts.receipt("attempt-2")).thenReturn(secondReceipt);
        doAnswer(
                        call -> {
                            assertActive();
                            return null;
                        })
                .when(completion)
                .finishReceipt(same(first), any(Turn.class), same(firstReceipt));
        var failure = new IllegalStateException("second receipt mismatch");
        doAnswer(
                        call -> {
                            assertActive();
                            throw failure;
                        })
                .when(completion)
                .finishReceipt(same(second), any(Turn.class), same(secondReceipt));
        assertThatThrownBy(recovery::recoverExpired).isSameAs(failure);
        var order = inOrder(commands, turns, receipts, completion);
        order.verify(commands).nextExpiredCommand();
        order.verify(turns).lockTurn("turn");
        order.verify(receipts).receipt("attempt-1");
        order.verify(completion).finishReceipt(same(first), any(Turn.class), same(firstReceipt));
        order.verify(commands).nextExpiredCommand();
        order.verify(turns).lockTurn("turn");
        order.verify(receipts).receipt("attempt-2");
        order.verify(completion).finishReceipt(same(second), any(Turn.class), same(secondReceipt));
        order.verifyNoMoreInteractions();
        verifyNoInteractions(transitions);
        assertThat(manager.begins).isEqualTo(2);
        assertThat(manager.commits).isOne();
        assertThat(manager.rollbacks).isOne();
    }

    private static Command command(int i) {
        return new Command("command-" + i, "turn", "running", "attempt-" + i, 1, false, null);
    }

    private static Turn turn() {
        return new Turn(
                "turn",
                "conversation",
                "tenant",
                "alice",
                "key",
                "hash",
                "message",
                "release",
                "running",
                2,
                null,
                null);
    }

    private static void assertActive() {
        assertThat(TransactionSynchronizationManager.isActualTransactionActive()).isTrue();
    }

    private static final class LocalTransactions extends AbstractPlatformTransactionManager {
        private int begins, commits, rollbacks;

        @Override
        protected Object doGetTransaction() {
            return new Object();
        }

        @Override
        protected void doBegin(Object transaction, TransactionDefinition definition) {
            begins++;
        }

        @Override
        protected void doCommit(DefaultTransactionStatus status) {
            assertActive();
            commits++;
        }

        @Override
        protected void doRollback(DefaultTransactionStatus status) {
            assertActive();
            rollbacks++;
        }
    }
}
