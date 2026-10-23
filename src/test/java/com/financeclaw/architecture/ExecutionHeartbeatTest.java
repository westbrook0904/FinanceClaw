package com.financeclaw.architecture;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;

import com.financeclaw.app.execution.ExecutionLeaseService;
import com.financeclaw.app.task.port.TaskQueryGateway;
import com.financeclaw.domain.Task.*;
import com.financeclaw.infrastructure.task.MyBatisCommandGateway;
import com.financeclaw.infrastructure.task.TaskMapper;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.support.SimpleTransactionStatus;
import org.springframework.transaction.support.TransactionTemplate;

class ExecutionHeartbeatTest {
  private static final Claim CLAIM =
      new Claim(
          new Command("command", "turn", "running", "attempt", 1, true, null),
          new Turn(
              "turn",
              "conversation",
              "tenant",
              "alice",
              "key",
              "hash",
              "message",
              "release",
              "running",
              1,
              null,
              null));
  private final TaskMapper mapper = mock(TaskMapper.class);
  private final TaskQueryGateway reads = mock(TaskQueryGateway.class);
  private final PlatformTransactionManager manager = mock(PlatformTransactionManager.class);
  private ExecutionLeaseService leases;

  @BeforeEach
  void setup() {
    when(manager.getTransaction(any())).thenAnswer(ignored -> new SimpleTransactionStatus());
    leases =
        new ExecutionLeaseService(
            new MyBatisCommandGateway(mapper), null, reads, null, new TransactionTemplate(manager));
  }

  @ParameterizedTest
  @ValueSource(booleans = {false, true})
  void heartbeatUsesExactlyOneMapperStatementForRenewalAndCancellation(boolean cancelling) {
    when(mapper.updateCommandLeaseAndGetCancellation(CLAIM, 15000)).thenReturn(cancelling);

    assertEquals(cancelling, leases.heartbeat(CLAIM, 15000));

    var order = inOrder(manager, mapper);
    order.verify(manager).getTransaction(any());
    order.verify(mapper).updateCommandLeaseAndGetCancellation(CLAIM, 15000);
    order.verify(manager).commit(any());
    order.verifyNoMoreInteractions();
    verifyNoInteractions(reads);
  }

  @Test
  void noMatchingLeaseIsFencedWithoutFallbackQueries() {
    when(mapper.updateCommandLeaseAndGetCancellation(CLAIM, 15000)).thenReturn(null);
    assertThrows(Fenced.class, () -> leases.heartbeat(CLAIM, 15000));

    verify(mapper).updateCommandLeaseAndGetCancellation(CLAIM, 15000);
    verifyNoMoreInteractions(mapper);
    verifyNoInteractions(reads);
    verify(manager).rollback(any());
  }

  @ParameterizedTest
  @ValueSource(longs = {0, -1})
  void invalidLeaseDoesNotOpenATransaction(long lease) {
    assertThrows(IllegalArgumentException.class, () -> leases.heartbeat(CLAIM, lease));
    verifyNoInteractions(mapper, reads, manager);
  }
}
