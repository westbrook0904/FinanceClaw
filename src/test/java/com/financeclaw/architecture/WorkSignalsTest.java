package com.financeclaw.architecture;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

import com.financeclaw.app.task.TaskEventHub;
import com.financeclaw.app.task.WorkSignals;
import com.financeclaw.app.task.WorkSignals.Queue;
import com.financeclaw.domain.Task.*;
import com.financeclaw.infrastructure.task.*;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.springframework.transaction.TransactionDefinition;
import org.springframework.transaction.support.AbstractPlatformTransactionManager;
import org.springframework.transaction.support.DefaultTransactionStatus;
import org.springframework.transaction.support.TransactionTemplate;

class WorkSignalsTest {
    private final WorkSignals signals = new WorkSignals();
    private final TransactionTemplate tx = new TransactionTemplate(new LocalTransactions());

    @Test
    void wakesCommandQueueOnlyAfterCommit() {
        tx.executeWithoutResult(
                status -> {
                    signals.afterCommit(Queue.COMMAND);
                    assertThat(signals.generation(Queue.COMMAND)).isZero();
                });
        assertThat(signals.generation(Queue.COMMAND)).isEqualTo(1);
    }

    @Test
    void rolledBackTransactionCannotWakeCommandQueue() {
        tx.executeWithoutResult(
                status -> {
                    signals.afterCommit(Queue.COMMAND);
                    status.setRollbackOnly();
                });
        assertThat(signals.generation(Queue.COMMAND)).isZero();
    }

    @Test
    void admissionWakeParticipatesInTheWholeBusinessTransaction() {
        var mapper = mock(TaskMapper.class);
        var turn = mock(Turn.class);
        when(turn.id()).thenReturn("turn");
        when(mapper.insertTurnEventAndReturn("turn"))
                .thenReturn(new Event("turn", 1, "queued", null));
        var gateway = new MyBatisTaskAdmissionGateway(mapper, new TaskEventHub(), signals);
        tx.executeWithoutResult(
                status -> {
                    gateway.recordAdmission(turn, "command");
                    assertThat(signals.generation(Queue.COMMAND)).isZero();
                    status.setRollbackOnly();
                });
        assertThat(signals.generation(Queue.COMMAND)).isZero();
        tx.executeWithoutResult(status -> gateway.recordAdmission(turn, "command"));
        assertThat(signals.generation(Queue.COMMAND)).isEqualTo(1);
    }

    @Test
    void noticeAndCommandGenerationsAreIndependent() {
        signals.afterCommit(Queue.NOTICE);
        assertThat(signals.generation(Queue.NOTICE)).isEqualTo(1);
        assertThat(signals.generation(Queue.COMMAND)).isZero();
        signals.afterCommit(Queue.COMMAND);
        assertThat(signals.generation(Queue.COMMAND)).isEqualTo(1);
        assertThat(signals.generation(Queue.NOTICE)).isEqualTo(1);
    }

    @ParameterizedTest
    @ValueSource(strings = {"waiting", "blocked", "completed", "failed", "cancelled"})
    void wakesNoticeQueueOnlyAfterARelevantTaskCommit(String state) {
        tx.executeWithoutResult(
                status -> {
                    signals.afterTaskCommit(state);
                    assertThat(signals.generation(Queue.NOTICE)).isZero();
                });
        assertThat(signals.generation(Queue.NOTICE)).isEqualTo(1);
        assertThat(signals.generation(Queue.COMMAND)).isZero();
    }

    @ParameterizedTest
    @ValueSource(strings = {"queued", "resuming", "running", "cancelling"})
    void inProgressTaskCommitDoesNotWakeNoticeQueue(String state) {
        tx.executeWithoutResult(status -> signals.afterTaskCommit(state));
        assertThat(signals.generation(Queue.NOTICE)).isZero();
        assertThat(signals.generation(Queue.COMMAND)).isZero();
    }

    @Test
    void rolledBackTaskTransitionCannotWakeNoticeQueue() {
        tx.executeWithoutResult(
                status -> {
                    signals.afterTaskCommit("completed");
                    status.setRollbackOnly();
                });
        assertThat(signals.generation(Queue.NOTICE)).isZero();
        assertThat(signals.generation(Queue.COMMAND)).isZero();
    }

    @Test
    void mutationUsesPersistedEventStatusAndSharesItsBusinessTransaction() {
        var mapper = mock(TaskMapper.class);
        when(mapper.insertTurnEventAndReturn("turn"))
                .thenReturn(new Event("turn", 3, "completed", null));
        var gateway = new MyBatisTaskMutationGateway(mapper, new TaskEventHub(), signals);
        tx.executeWithoutResult(
                status -> {
                    gateway.event("turn");
                    assertThat(signals.generation(Queue.NOTICE)).isZero();
                    status.setRollbackOnly();
                });
        assertThat(signals.generation(Queue.NOTICE)).isZero();
        tx.executeWithoutResult(status -> gateway.event("turn"));
        assertThat(signals.generation(Queue.NOTICE)).isEqualTo(1);
        when(mapper.insertTurnEventAndReturn("turn"))
                .thenReturn(new Event("turn", 4, "running", null));
        tx.executeWithoutResult(status -> gateway.event("turn"));
        assertThat(signals.generation(Queue.NOTICE)).isEqualTo(1);
        assertThat(signals.generation(Queue.COMMAND)).isZero();
    }

    private static final class LocalTransactions extends AbstractPlatformTransactionManager {
        protected Object doGetTransaction() {
            return new Object();
        }

        protected void doBegin(Object transaction, TransactionDefinition definition) {}

        protected void doCommit(DefaultTransactionStatus status) {}

        protected void doRollback(DefaultTransactionStatus status) {}
    }
}
