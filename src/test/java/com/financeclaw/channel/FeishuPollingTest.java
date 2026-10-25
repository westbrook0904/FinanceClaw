package com.financeclaw.channel;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;

import com.financeclaw.app.execution.port.ExecutionContextGateway;
import com.financeclaw.app.task.TaskQueryService;
import com.financeclaw.app.task.WorkSignals;
import com.financeclaw.domain.Feishu.Binding;
import com.financeclaw.domain.Feishu.Notice;
import com.financeclaw.infrastructure.channel.FeishuMapper;
import com.financeclaw.worker.IdlePoll;
import java.util.List;
import java.util.concurrent.*;
import org.junit.jupiter.api.Test;
import org.springframework.transaction.support.SimpleTransactionStatus;
import org.springframework.transaction.support.TransactionCallback;
import org.springframework.transaction.support.TransactionTemplate;

class FeishuPollingTest {
    private final FeishuMapper db = mock(FeishuMapper.class);
    private final IdlePoll idle = new IdlePoll(1000, 5000);
    private final WorkSignals signals = new WorkSignals();
    private final TransactionTemplate tx = mock(TransactionTemplate.class);

    private FeishuDelivery delivery(boolean enabled) {
        return new FeishuDelivery(
                db,
                mock(TaskQueryService.class),
                mock(ExecutionContextGateway.class),
                tx,
                mock(AgentScopeFeishu.class),
                signals,
                enabled,
                idle);
    }

    @Test
    void noticeCommitDuringAnEmptyScanCannotBeHiddenByItsOldBackoffTicket() {
        var delivery = delivery(true);
        when(db.listBindingIdsRequiringProjection())
                .thenAnswer(
                        ignored -> {
                            signals.afterCommit(WorkSignals.Queue.NOTICE);
                            return List.of();
                        });
        delivery.scheduled();
        delivery.scheduled();
        verify(db, times(2)).listBindingIdsRequiringProjection();
        verify(db, times(2)).getNextPendingNotice();
    }

    @Test
    void onlyTheNoticeQueueCanWakeNotificationBackoff() {
        var delivery = delivery(true);
        when(db.listBindingIdsRequiringProjection()).thenReturn(List.of());
        delivery.scheduled();
        signals.afterCommit(WorkSignals.Queue.COMMAND);
        delivery.scheduled();
        verify(db, times(1)).listBindingIdsRequiringProjection();
        signals.afterCommit(WorkSignals.Queue.NOTICE);
        delivery.scheduled();
        verify(db, times(2)).listBindingIdsRequiringProjection();
    }

    @Test
    void suppressingAStaleCandidateCountsAsWorkInsteadOfStartingIdleBackoff() {
        var delivery = delivery(true);
        var stale = new Notice("notice", "turn", "binding", 1, 2, "payload", "pending", null);
        var muted =
                new Binding(
                        "binding",
                        "channel-tenant",
                        "user",
                        "chat",
                        "tenant",
                        "subject",
                        "conversation",
                        1,
                        true,
                        "turn");
        when(tx.execute(any()))
                .thenAnswer(
                        call ->
                                call.<TransactionCallback<?>>getArgument(0)
                                        .doInTransaction(new SimpleTransactionStatus()));
        when(db.listBindingIdsRequiringProjection()).thenReturn(List.of());
        var candidate = new FeishuMapper.NoticeCandidate(stale.id(), stale.bindingId());
        when(db.getNextPendingNotice()).thenReturn(candidate, null, null);
        when(db.getBindingAndNoticeForUpdate("binding", "notice"))
                .thenReturn(
                        new FeishuMapper.LockedNoticeRow(
                                muted.id(),
                                muted.tenantKey(),
                                muted.openId(),
                                muted.chatId(),
                                muted.tenant(),
                                muted.subject(),
                                muted.conversationId(),
                                muted.revision(),
                                muted.muted(),
                                muted.activeTurn(),
                                stale.id(),
                                stale.turnId(),
                                stale.bindingId(),
                                stale.bindingRevision(),
                                stale.revision(),
                                stale.payload(),
                                stale.status(),
                                stale.reason()));
        delivery.scheduled();
        delivery.scheduled();
        verify(db).updateNoticeOutcome("notice", "suppressed", "binding_changed");
        verify(db, times(2)).listBindingIdsRequiringProjection();
        verify(db, times(3)).getNextPendingNotice();
        verify(db, never()).updatePendingNoticeSending(anyString());
    }

    @Test
    void missingLockedClaimRowsCannotWriteOrSendFromCandidateIdentifiers() {
        var tasks = mock(TaskQueryService.class);
        var interactions = mock(ExecutionContextGateway.class);
        var channel = mock(AgentScopeFeishu.class);
        var delivery =
                new FeishuDelivery(db, tasks, interactions, tx, channel, signals, true, idle);
        var observed = new FeishuMapper.NoticeCandidate("notice", "binding");
        when(tx.execute(any()))
                .thenAnswer(
                        call ->
                                call.<TransactionCallback<?>>getArgument(0)
                                        .doInTransaction(new SimpleTransactionStatus()));
        when(db.listBindingIdsRequiringProjection()).thenReturn(List.of());
        when(db.getNextPendingNotice()).thenReturn(observed, null);
        when(db.getBindingAndNoticeForUpdate("binding", "notice")).thenReturn(null);

        delivery.scheduled();

        verify(db).listBindingIdsRequiringProjection();
        verify(db, times(2)).getNextPendingNotice();
        verify(db).getBindingAndNoticeForUpdate("binding", "notice");
        // Candidate identifiers cannot substitute for missing locked rows or authorize side
        // effects.
        verifyNoMoreInteractions(db);
        verifyNoInteractions(tasks, interactions, channel);
        assertEquals(0, signals.generation(WorkSignals.Queue.NOTICE));
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
