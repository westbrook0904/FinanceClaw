package com.financeclaw.app.execution;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

import com.financeclaw.app.execution.port.CommandGateway;
import com.financeclaw.app.execution.port.ExecutionReceiptGateway;
import com.financeclaw.app.task.TaskTransitionService;
import com.financeclaw.app.task.port.TaskMutationGateway;
import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.Task.*;
import java.util.List;
import java.util.stream.Stream;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.Arguments;
import org.junit.jupiter.params.provider.MethodSource;
import org.junit.jupiter.params.provider.ValueSource;
import org.springframework.transaction.TransactionDefinition;
import org.springframework.transaction.support.AbstractPlatformTransactionManager;
import org.springframework.transaction.support.DefaultTransactionStatus;
import org.springframework.transaction.support.TransactionSynchronizationManager;
import org.springframework.transaction.support.TransactionTemplate;

/**
 * 验证同次读取的回执如何在持有事务的用例间传递，以及传入值的身份绑定和检查顺序。 本测试只有 Spring 事务同步，不使用数据库；真实行锁、SQL 数量及写入原子性另由 PostgreSQL
 * 集成测试验证。
 */
class ExecutionCompletionReceiptTest {
    private static final Command CURRENT =
            new Command("command", "turn", "running", "attempt", 7, true, null);
    private static final Turn RUNNING = turn("running");
    private static final Claim CLAIM = new Claim(CURRENT, RUNNING);
    private static final Receipt SEALED =
            new Receipt("attempt", "command", 7, 3L, "completed", "answer");
    private final CommandGateway commands = mock(CommandGateway.class);
    private final TaskMutationGateway turns = mock(TaskMutationGateway.class);
    private final ExecutionReceiptGateway receipts = mock(ExecutionReceiptGateway.class);
    private final ExecutionPolicyService policies = mock(ExecutionPolicyService.class);
    private final TaskTransitionService transitions = mock(TaskTransitionService.class);
    private final LocalTransactions manager = new LocalTransactions();
    private final TransactionTemplate tx = new TransactionTemplate(manager);
    private ExecutionCompletionService completion;

    @BeforeEach
    void setup() {
        // 对每个持久边界检查真实 Spring 事务标志，防止把读取移动到 fenced/恢复事务之外。
        doAnswer(call -> inTransaction(CURRENT)).when(commands).lockCommand("command");
        doAnswer(call -> inTransaction(RUNNING)).when(turns).lockTurn("turn");
        doAnswer(call -> inTransaction(SEALED)).when(receipts).receipt("attempt");
        doAnswer(call -> inTransaction(true))
                .when(receipts)
                .matchesCheckpoint(any(Claim.class), anyLong());
        doAnswer(call -> inTransaction(null)).when(policies).checkPolicy(any(Claim.class));
        doAnswer(call -> inTransaction(null))
                .when(transitions)
                .change(
                        any(Turn.class),
                        anyString(),
                        nullable(String.class),
                        nullable(String.class));
        doAnswer(call -> inTransaction(null)).when(commands).finishCommand("command");
        var leases = new ExecutionLeaseService(commands, turns, transitions, tx);
        completion =
                new ExecutionCompletionService(
                        turns, commands, receipts, leases, policies, transitions);
    }

    @Test
    void normalFinishReadsOnceAfterTheOriginalLocksInsideTheFenceTransaction() {
        completion.finish(CLAIM);

        var order = inOrder(commands, turns, receipts, policies, transitions);
        // 第一次 command 锁来自 leases.fenced 的实时租约检查；本轮不删除第二次原有锁定。
        order.verify(commands, times(2)).lockCommand("command");
        order.verify(turns).lockTurn("turn");
        order.verify(receipts).receipt("attempt");
        order.verify(receipts).matchesCheckpoint(CLAIM, 3);
        order.verify(policies).checkPolicy(CLAIM);
        order.verify(transitions).change(RUNNING, "completed", "answer", null);
        order.verify(commands).finishCommand("command");
        order.verifyNoMoreInteractions();
        assertCommitted();
    }

    @Test
    void recoveryPassesTheSameSealedReceiptWithoutReloadingItInsideTheTransaction() {
        var expired = new Command("command", "turn", "running", "attempt", 7, false, null);
        doAnswer(
                        call -> {
                            assertThat(
                                            TransactionSynchronizationManager
                                                    .isActualTransactionActive())
                                    .isFalse();
                            return List.of("command");
                        })
                .when(commands)
                .expired();
        doAnswer(call -> inTransaction(expired)).when(commands).lockCommand("command");
        var finishing = spy(completion);
        var recovery =
                new RecoverTaskService(commands, turns, receipts, finishing, transitions, tx);

        assertThat(recovery.recoverExpired()).isOne();

        verify(finishing).finishReceipt(same(expired), same(RUNNING), same(SEALED));
        var order = inOrder(commands, turns, receipts, policies, transitions);
        order.verify(commands).expired();
        order.verify(commands).lockCommand("command");
        order.verify(turns).lockTurn("turn");
        order.verify(receipts).receipt("attempt");
        order.verify(receipts).matchesCheckpoint(new Claim(expired, RUNNING), 3);
        order.verify(policies).checkPolicy(new Claim(expired, RUNNING));
        order.verify(transitions).change(RUNNING, "completed", "answer", null);
        order.verify(commands).finishCommand("command");
        order.verifyNoMoreInteractions();
        assertCommitted();
    }

    @ParameterizedTest(name = "{0}")
    @MethodSource("unusableReceipts")
    void suppliedReceiptMustMatchTheAttemptCommandFenceAndSealedOutcome(
            String label, Receipt receipt) {
        assertThatThrownBy(
                        () ->
                                tx.executeWithoutResult(
                                        status ->
                                                completion.finishReceipt(
                                                        CURRENT, RUNNING, receipt)))
                .isInstanceOf(IllegalStateException.class)
                .hasMessage("Missing sealed execution receipt");

        // 主键读取原有的隐含绑定现在显式检查；wrong commandId 是本轮新增防护。
        // 无论哪个字段失败，都不能继续读取检查点、判断策略、改变状态或重新查询回执。
        verifyNoInteractions(receipts, policies, transitions, commands, turns);
        assertThat(manager.begins).isOne();
        assertThat(manager.commits).isZero();
        assertThat(manager.rollbacks).isOne();
        assertThat(TransactionSynchronizationManager.isActualTransactionActive()).isFalse();
    }

    @Test
    void policyRejectionKeepsPrecedenceAfterTheCheckpointLookupAlreadyRejected() {
        doAnswer(call -> inTransaction(false)).when(receipts).matchesCheckpoint(CLAIM, 3);
        doAnswer(
                        call -> {
                            inTransaction(null);
                            throw new Blocked("authorization_changed");
                        })
                .when(policies)
                .checkPolicy(CLAIM);

        tx.executeWithoutResult(status -> completion.finishReceipt(CURRENT, RUNNING, SEALED));

        var order = inOrder(receipts, policies, transitions, commands);
        order.verify(receipts).matchesCheckpoint(CLAIM, 3);
        order.verify(policies).checkPolicy(CLAIM);
        order.verify(transitions).change(RUNNING, "blocked", null, "authorization_changed");
        order.verify(commands).finishCommand("command");
        order.verifyNoMoreInteractions();
        verify(receipts, never()).receipt(anyString());
        assertCommitted();
    }

    @ParameterizedTest
    @ValueSource(booleans = {true, false})
    void cancellingSkipsPolicyButStillRequiresTheMatchingCheckpoint(boolean matching) {
        var cancelling = turn("cancelling");
        var claim = new Claim(CURRENT, cancelling);
        doAnswer(call -> inTransaction(matching)).when(receipts).matchesCheckpoint(claim, 3);

        tx.executeWithoutResult(status -> completion.finishReceipt(CURRENT, cancelling, SEALED));

        var order = inOrder(receipts, transitions, commands);
        order.verify(receipts).matchesCheckpoint(claim, 3);
        order.verify(transitions)
                .change(
                        cancelling,
                        matching ? "cancelled" : "blocked",
                        null,
                        matching ? "cancel_confirmed" : "checkpoint_mismatch");
        order.verify(commands).finishCommand("command");
        order.verifyNoMoreInteractions();
        verifyNoInteractions(policies);
        verify(receipts, never()).receipt(anyString());
        assertCommitted();
    }

    private static Stream<Arguments> unusableReceipts() {
        return Stream.of(
                Arguments.of("missing", null),
                Arguments.of("unsealed", new Receipt("attempt", "command", 7, null, null, null)),
                Arguments.of(
                        "wrong attempt",
                        new Receipt("other", "command", 7, 3L, "completed", "answer")),
                Arguments.of(
                        "wrong command",
                        new Receipt("attempt", "other", 7, 3L, "completed", "answer")),
                Arguments.of(
                        "wrong fence",
                        new Receipt("attempt", "command", 6, 3L, "completed", "answer")));
    }

    private void assertCommitted() {
        assertThat(manager.begins).isOne();
        assertThat(manager.commits).isOne();
        assertThat(manager.rollbacks).isZero();
        assertThat(TransactionSynchronizationManager.isActualTransactionActive()).isFalse();
    }

    private static <T> T inTransaction(T value) {
        assertThat(TransactionSynchronizationManager.isActualTransactionActive()).isTrue();
        return value;
    }

    private static Turn turn(String status) {
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
                2,
                null,
                null);
    }

    /** 仅供单元验证事务包围关系，不模拟数据库提交、回滚或行锁。 */
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
