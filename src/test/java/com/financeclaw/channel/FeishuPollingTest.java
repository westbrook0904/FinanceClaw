package com.financeclaw.channel;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;

import com.financeclaw.app.execution.port.ExecutionContextGateway;
import com.financeclaw.app.task.TaskQueryService;
import com.financeclaw.infrastructure.channel.FeishuMapper;
import com.financeclaw.worker.IdlePoll;
import java.util.List;
import java.util.concurrent.*;
import org.junit.jupiter.api.Test;
import org.springframework.transaction.support.TransactionTemplate;

class FeishuPollingTest {
  private final FeishuMapper db = mock(FeishuMapper.class);
  private final IdlePoll idle = new IdlePoll(1000, 5000);

  private FeishuDelivery delivery(boolean enabled) {
    return new FeishuDelivery(
        db,
        mock(TaskQueryService.class),
        mock(ExecutionContextGateway.class),
        mock(TransactionTemplate.class),
        mock(AgentScopeFeishu.class),
        enabled,
        idle);
  }

  @Test
  void idleTicksDoNoSqlWhileRecoveryKeepsItsOwnSchedule() {
    var delivery = delivery(true);
    when(db.listBindingIdsRequiringProjection()).thenReturn(List.of());
    delivery.scheduled();
    delivery.scheduled();
    delivery.scheduled();
    verify(db, times(1)).listBindingIdsRequiringProjection();
    verify(db, times(1)).getNextPendingNotice();
    verify(db, never()).updateExpiredSendingNoticesUnknown();

    delivery.scheduledRecovery();
    verify(db, times(1)).updateExpiredSendingNoticesUnknown();
    delivery.scheduled();
    verify(db, times(1)).listBindingIdsRequiringProjection();

    idle.wake();
    delivery.scheduled();
    verify(db, times(2)).listBindingIdsRequiringProjection();
  }

  @Test
  void disabledScheduledDeliveryDoesNotQueryOrRecover() {
    var delivery = delivery(false);
    delivery.scheduled();
    delivery.scheduledRecovery();
    verifyNoInteractions(db);
  }

  @Test
  void aFailedScanBacksOffAndReleasesTheLocalPollingGuard() {
    var delivery = delivery(true);
    when(db.listBindingIdsRequiringProjection())
        .thenThrow(new IllegalStateException("database_unavailable"))
        .thenReturn(List.of());
    assertThrows(IllegalStateException.class, delivery::scheduled);
    delivery.scheduled();
    verify(db, times(1)).listBindingIdsRequiringProjection();
    idle.wake();
    delivery.scheduled();
    verify(db, times(2)).listBindingIdsRequiringProjection();
    verify(db, times(1)).getNextPendingNotice();
  }

  @Test
  void overlappingScheduledCallbacksDoNotDuplicateTheScan() throws Exception {
    var delivery = delivery(true);
    var entered = new CountDownLatch(1);
    var release = new CountDownLatch(1);
    when(db.listBindingIdsRequiringProjection())
        .thenAnswer(
            ignored -> {
              entered.countDown();
              assertTrue(release.await(10, TimeUnit.SECONDS));
              return List.of();
            });
    try (var pool = Executors.newVirtualThreadPerTaskExecutor()) {
      var first = pool.submit(delivery::scheduled);
      try {
        assertTrue(entered.await(10, TimeUnit.SECONDS));
        delivery.scheduled();
        verify(db, times(1)).listBindingIdsRequiringProjection();
      } finally {
        release.countDown();
      }
      first.get(10, TimeUnit.SECONDS);
    }
    verify(db, times(1)).getNextPendingNotice();
  }

  @Test
  void explicitMaintenancePollingBypassesIdleWithoutChangingUncertainRecovery() {
    var delivery = delivery(true);
    when(db.listBindingIdsRequiringProjection()).thenReturn(List.of());
    delivery.scheduled();
    delivery.poll();
    verify(db, times(2)).listBindingIdsRequiringProjection();
    verify(db, times(2)).getNextPendingNotice();
    verify(db, times(1)).updateExpiredSendingNoticesUnknown();
    verify(db, never()).updatePendingNoticeSending(anyString());
  }
}
