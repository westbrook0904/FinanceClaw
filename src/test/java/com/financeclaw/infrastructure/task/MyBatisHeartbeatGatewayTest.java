package com.financeclaw.infrastructure.task;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.Mockito.*;

import com.financeclaw.app.execution.ExecutionLeaseService;
import com.financeclaw.app.execution.port.CommandGateway;
import com.financeclaw.app.execution.port.HeartbeatGateway;
import com.financeclaw.domain.Task.*;
import java.lang.reflect.Method;
import org.apache.ibatis.annotations.Options;
import org.apache.ibatis.annotations.Select;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.support.SimpleTransactionStatus;
import org.springframework.transaction.support.TransactionTemplate;

class MyBatisHeartbeatGatewayTest {
    private static final Claim CLAIM =
            new Claim(
                    new Command("command", "turn", "running", "attempt", 1, true, null),
                    new Turn(
                            "turn",
                            "conversation",
                            "tenant",
                            "subject",
                            "key",
                            "hash",
                            "question",
                            "release",
                            "running",
                            1,
                            null,
                            null));

    @Test
    void dedicatedMapperSharesOriginalSqlAndDataAffectingOptions() throws Exception {
        Method original =
                TaskMapper.class.getMethod(
                        "updateCommandLeaseAndGetCancellation", Claim.class, long.class);
        Method dedicated =
                HeartbeatMapper.class.getMethod(
                        "updateCommandLeaseAndGetCancellation", Claim.class, long.class);
        assertThat(dedicated.getAnnotation(Select.class).value())
                .containsExactly(TaskMapper.RENEW_COMMAND_LEASE);
        assertThat(dedicated.getAnnotation(Select.class).value())
                .containsExactly(original.getAnnotation(Select.class).value());
        assertThat(dedicated.getAnnotation(Select.class).affectData()).isTrue();
        assertThat(dedicated.getAnnotation(Options.class).useCache()).isFalse();
        assertThat(dedicated.getAnnotation(Options.class).flushCache())
                .isEqualTo(Options.FlushCachePolicy.TRUE);
    }

    @ParameterizedTest
    @ValueSource(booleans = {true, false})
    void fiveArgumentServiceNeverOpensBusinessTransactionForHeartbeat(boolean cancelling) {
        var commands = mock(CommandGateway.class);
        var manager = mock(PlatformTransactionManager.class);
        var gateway = mock(HeartbeatGateway.class);
        when(gateway.renew(CLAIM, 15000)).thenReturn(cancelling);
        var service =
                new ExecutionLeaseService(
                        commands, null, null, new TransactionTemplate(manager), gateway);
        assertThat(service.heartbeat(CLAIM, 15000)).isEqualTo(cancelling);
        verify(gateway).renew(CLAIM, 15000);
        verifyNoInteractions(manager, commands);
    }

    @Test
    void dedicatedFailurePropagatesWithoutBusinessFallback() {
        var commands = mock(CommandGateway.class);
        var manager = mock(PlatformTransactionManager.class);
        var gateway = mock(HeartbeatGateway.class);
        var failure = new IllegalStateException("synthetic");
        when(gateway.renew(CLAIM, 15000)).thenThrow(failure);
        var service =
                new ExecutionLeaseService(
                        commands, null, null, new TransactionTemplate(manager), gateway);
        assertThatThrownBy(() -> service.heartbeat(CLAIM, 15000)).isSameAs(failure);
        verifyNoInteractions(manager, commands);
    }

    @Test
    void explicitFixtureConstructorStillRollsBackNullInsideItsTransaction() {
        var commands = mock(CommandGateway.class);
        when(commands.renew(CLAIM, 15000)).thenReturn(null);
        var manager = mock(PlatformTransactionManager.class);
        when(manager.getTransaction(any())).thenAnswer(ignored -> new SimpleTransactionStatus());
        var service =
                new ExecutionLeaseService(commands, null, null, new TransactionTemplate(manager));
        assertThatThrownBy(() -> service.heartbeat(CLAIM, 15000)).isInstanceOf(Fenced.class);
        verify(manager).rollback(any());
        verify(manager, never()).commit(any());
    }
}
