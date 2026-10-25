package com.financeclaw.app.task;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

import com.financeclaw.app.execution.port.ExecutionMemory;
import com.financeclaw.app.execution.port.ExecutionPolicyGateway;
import com.financeclaw.app.task.port.TaskMutationGateway;
import com.financeclaw.domain.Task.*;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.springframework.transaction.TransactionDefinition;
import org.springframework.transaction.support.AbstractPlatformTransactionManager;
import org.springframework.transaction.support.DefaultTransactionStatus;
import org.springframework.transaction.support.TransactionSynchronizationManager;
import org.springframework.transaction.support.TransactionTemplate;

/**
 * 窄双入口的应用契约：普通写入不取回 Turn，领取写入返回原始行，两者共享原事务及副作用顺序。 这里只验证真实 Spring 事务同步和回滚信号，不模拟 PostgreSQL
 * 行锁、RETURNING 或数据库原子性。
 */
class TaskTransitionServiceTest {
    private static final String BODY = "completed report body ".repeat(1024);
    private static final Turn RUNNING = turn("running", 2, null);
    private static final Turn STORED = turn("completed", 9, "database-side payload");
    private final TaskMutationGateway db = mock(TaskMutationGateway.class);
    private final ExecutionPolicyGateway governance = mock(ExecutionPolicyGateway.class);
    private final ExecutionMemory memory = mock(ExecutionMemory.class);
    private final LocalTransactions manager = new LocalTransactions();
    private final TransactionTemplate tx = new TransactionTemplate(manager);
    private final TaskTransitionService service = new TaskTransitionService(db, governance, memory);

    @BeforeEach
    void setup() {
        doAnswer(call -> inTransaction(null)).when(governance).invalidate("turn");
        doAnswer(call -> inTransaction(null))
                .when(db)
                .transition(
                        anyString(), anyString(), nullable(String.class), nullable(String.class));
        doAnswer(call -> inTransaction(STORED))
                .when(db)
                .transitionAndReturn(
                        anyString(), anyString(), nullable(String.class), nullable(String.class));
        doAnswer(call -> inTransaction(null)).when(db).event("turn");
        doAnswer(call -> inTransaction(null))
                .when(db)
                .journal(eq("conversation"), eq("turn"), eq("assistant"), anyString());
        doAnswer(call -> inTransaction(null)).when(memory).completed("turn");
    }

    @ParameterizedTest(name = "returning={0}")
    @ValueSource(booleans = {false, true})
    void completedTransitionKeepsWriteEntrypointsSeparateAndUsesOriginalSideEffectArguments(
            boolean returning) {
        Turn actual = tx.execute(status -> invoke(returning, RUNNING, "completed", BODY, "reason"));

        if (returning) {
            // 不根据输入重建 Turn；即使行中有数据库侧投影，也必须原样返回同一个对象。
            assertThat(actual).isSameAs(STORED);
        } else {
            assertThat(actual).isNull();
        }
        var order = inOrder(governance, db, memory);
        order.verify(governance).invalidate("turn");
        if (returning) {
            order.verify(db).transitionAndReturn("turn", "completed", BODY, "reason");
            verify(db, never()).transition(anyString(), anyString(), any(), any());
        } else {
            order.verify(db).transition("turn", "completed", BODY, "reason");
            // 普通大结果写入不走返回完整正文的入口，也不追加任何回读。
            verify(db, never()).transitionAndReturn(anyString(), anyString(), any(), any());
        }
        order.verify(db).event("turn");
        order.verify(db).journal("conversation", "turn", "assistant", BODY);
        order.verify(memory).completed("turn");
        order.verifyNoMoreInteractions();
        assertCommitted();
    }

    @Test
    void runningReturningTransitionReturnsTheActualRowWithoutInvalidationJournalOrMemory() {
        var queued = turn("queued", 1, null);
        var stored = turn("running", 2, null);
        doAnswer(call -> inTransaction(stored))
                .when(db)
                .transitionAndReturn("turn", "running", null, null);

        Turn actual = tx.execute(status -> service.changeAndReturn(queued, "running", null, null));

        assertThat(actual).isSameAs(stored);
        var order = inOrder(db);
        order.verify(db).transitionAndReturn("turn", "running", null, null);
        order.verify(db).event("turn");
        order.verifyNoMoreInteractions();
        verifyNoInteractions(governance, memory);
        assertCommitted();
    }

    @Test
    void missingReturningRowRollsBackBeforeEventJournalOrMemory() {
        doAnswer(call -> inTransaction(null))
                .when(db)
                .transitionAndReturn("turn", "completed", BODY, "reason");

        assertThatThrownBy(
                        () ->
                                tx.execute(
                                        status ->
                                                service.changeAndReturn(
                                                        RUNNING, "completed", BODY, "reason")))
                .isInstanceOf(Fenced.class);

        var order = inOrder(governance, db);
        // 交互失效发生在更新前，仍由原外层事务负责整体回滚；此处不宣称 mock 实现了数据回滚。
        order.verify(governance).invalidate("turn");
        order.verify(db).transitionAndReturn("turn", "completed", BODY, "reason");
        order.verifyNoMoreInteractions();
        verifyNoInteractions(memory);
        assertRolledBack();
    }

    @ParameterizedTest(name = "returning={0}")
    @ValueSource(booleans = {false, true})
    void bothEntrypointsCheckTransactionBeforeDomainRulesOrPersistentEffects(boolean returning) {
        var terminal = turn("completed", 3, "finished");

        // 终态输入故意与无事务同时出现：必须先报事务约定，而不是先抛领域 Fenced。
        assertThatThrownBy(() -> invoke(returning, terminal, "completed", BODY, null))
                .isInstanceOf(IllegalStateException.class)
                .hasMessage("Task transition requires a transaction");

        verifyNoInteractions(db, governance, memory);
        assertThat(manager.begins).isZero();
        assertThat(manager.commits).isZero();
        assertThat(manager.rollbacks).isZero();
        assertThat(TransactionSynchronizationManager.isActualTransactionActive()).isFalse();
    }

    @ParameterizedTest(name = "returning={0}")
    @ValueSource(booleans = {false, true})
    void bothEntrypointsRejectTerminalTurnBeforeInvalidationOrMutation(boolean returning) {
        var terminal = turn("completed", 3, "finished");

        assertThatThrownBy(
                        () ->
                                tx.execute(
                                        status ->
                                                invoke(
                                                        returning,
                                                        terminal,
                                                        "completed",
                                                        BODY,
                                                        null)))
                .isInstanceOf(Fenced.class);

        verifyNoInteractions(db, governance, memory);
        assertRolledBack();
    }

    private Turn invoke(boolean returning, Turn turn, String status, String result, String reason) {
        if (returning) {
            return service.changeAndReturn(turn, status, result, reason);
        }
        service.change(turn, status, result, reason);
        return null;
    }

    private void assertCommitted() {
        assertThat(manager.begins).isOne();
        assertThat(manager.commits).isOne();
        assertThat(manager.rollbacks).isZero();
        assertThat(TransactionSynchronizationManager.isActualTransactionActive()).isFalse();
    }

    private void assertRolledBack() {
        assertThat(manager.begins).isOne();
        assertThat(manager.commits).isZero();
        assertThat(manager.rollbacks).isOne();
        assertThat(TransactionSynchronizationManager.isActualTransactionActive()).isFalse();
    }

    private static <T> T inTransaction(T value) {
        assertThat(TransactionSynchronizationManager.isActualTransactionActive()).isTrue();
        return value;
    }

    private static Turn turn(String status, long revision, String result) {
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
                result,
                null);
    }

    /** 只管理单元测试的 Spring 事务标志，绝不代替真实数据库事务验收。 */
    private static final class LocalTransactions extends AbstractPlatformTransactionManager {
        private int begins;
        private int commits;
        private int rollbacks;

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
            inTransaction(null);
            commits++;
        }

        @Override
        protected void doRollback(DefaultTransactionStatus status) {
            inTransaction(null);
            rollbacks++;
        }
    }
}
