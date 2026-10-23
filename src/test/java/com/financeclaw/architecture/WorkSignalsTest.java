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

    private static final class LocalTransactions extends AbstractPlatformTransactionManager {
        protected Object doGetTransaction() {
            return new Object();
        }

        protected void doBegin(Object transaction, TransactionDefinition definition) {}

        protected void doCommit(DefaultTransactionStatus status) {}

        protected void doRollback(DefaultTransactionStatus status) {}
    }
}
