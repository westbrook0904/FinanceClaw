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
import java.util.concurrent.atomic.AtomicInteger;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.transaction.support.SimpleTransactionStatus;
import org.springframework.transaction.support.TransactionCallback;
import org.springframework.transaction.support.TransactionTemplate;

class FeishuPollingTest {
    private final FeishuMapper db = mock(FeishuMapper.class);
    private final IdlePoll idle = new IdlePoll(1000, 5000);
    private final WorkSignals signals = new WorkSignals();
    private final TransactionTemplate tx = mock(TransactionTemplate.class);
    private final TaskQueryService tasks = mock(TaskQueryService.class);
    private final ExecutionContextGateway interactions = mock(ExecutionContextGateway.class);
    private final AgentScopeFeishu channel = mock(AgentScopeFeishu.class);

    @BeforeEach
    void runTransactionCallbacks() {
        when(tx.execute(any()))
                .thenAnswer(
                        call ->
                                call.<TransactionCallback<?>>getArgument(0)
                                        .doInTransaction(new SimpleTransactionStatus()));
    }

    private FeishuDelivery delivery(boolean enabled) {
        return new FeishuDelivery(db, tasks, interactions, tx, channel, signals, enabled, idle);
    }

    @Test
    void noticeCommitDuringAnEmptyScanCannotBeHiddenByItsOldBackoffTicket() {
        var delivery = delivery(true);
        when(db.getNextBindingRequiringProjectionForUpdateSkipLocked(anyList()))
                .thenAnswer(
                        ignored -> {
                            signals.afterCommit(WorkSignals.Queue.NOTICE);
                            return null;
                        });
        delivery.scheduled();
        delivery.scheduled();
        verify(db, times(2)).getNextBindingRequiringProjectionForUpdateSkipLocked(List.of());
        verify(db, times(2)).getNextNoticeForUpdateSkipLocked(List.of());
    }

    @Test
    void onlyTheNoticeQueueCanWakeNotificationBackoff() {
        var delivery = delivery(true);
        delivery.scheduled();
        signals.afterCommit(WorkSignals.Queue.COMMAND);
        delivery.scheduled();
        verify(db).getNextBindingRequiringProjectionForUpdateSkipLocked(List.of());
        signals.afterCommit(WorkSignals.Queue.NOTICE);
        delivery.scheduled();
        verify(db, times(2)).getNextBindingRequiringProjectionForUpdateSkipLocked(List.of());
    }

    @Test
    void suppressingAStaleCandidateCountsAsWorkInsteadOfStartingIdleBackoff() {
        var delivery = delivery(true);
        var stale = new Notice("notice", "turn", "binding", 1, 2, "payload", "pending", null);
        when(db.getNextNoticeForUpdateSkipLocked(anyList()))
                .thenReturn(scheduled(binding("binding", true), stale), null, null);
        delivery.scheduled();
        delivery.scheduled();
        verify(db).updateNoticeOutcome("notice", "suppressed", "binding_changed");
        verify(db, times(2)).getNextBindingRequiringProjectionForUpdateSkipLocked(List.of());
        verify(db, times(3)).getNextNoticeForUpdateSkipLocked(List.of());
        verify(db, never()).updatePendingNoticeSending(anyString());
    }

    @Test
    void explicitMissingLockedClaimRowsCannotWriteOrSendFromCandidateIdentifiers() {
        var delivery = delivery(true);
        when(db.getNextPendingNotice())
                .thenReturn(new FeishuMapper.NoticeCandidate("notice", "binding"));
        when(db.getBindingAndNoticeForUpdate("binding", "notice")).thenReturn(null);
        assertNull(delivery.claim());
        verify(db).getNextPendingNotice();
        verify(db).getBindingAndNoticeForUpdate("binding", "notice");
        verifyNoMoreInteractions(db);
        verifyNoInteractions(tasks, interactions, channel);
        assertEquals(0, signals.generation(WorkSignals.Queue.NOTICE));
    }

    @Test
    void idleTicksDoNoSqlWhileRecoveryKeepsItsOwnSchedule() {
        var delivery = delivery(true);
        delivery.scheduled();
        delivery.scheduled();
        delivery.scheduled();
        verify(db).getNextBindingRequiringProjectionForUpdateSkipLocked(List.of());
        verify(db).getNextNoticeForUpdateSkipLocked(List.of());
        verify(db, never()).updateExpiredSendingNoticesUnknown();
        delivery.scheduledRecovery();
        verify(db).updateExpiredSendingNoticesUnknown();
        delivery.scheduled();
        verify(db).getNextBindingRequiringProjectionForUpdateSkipLocked(List.of());
        idle.wake();
        delivery.scheduled();
        verify(db, times(2)).getNextBindingRequiringProjectionForUpdateSkipLocked(List.of());
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
        when(db.getNextBindingRequiringProjectionForUpdateSkipLocked(anyList()))
                .thenThrow(new IllegalStateException("database_unavailable"))
                .thenReturn(null);
        assertThrows(IllegalStateException.class, delivery::scheduled);
        delivery.scheduled();
        verify(db).getNextBindingRequiringProjectionForUpdateSkipLocked(List.of());
        idle.wake();
        delivery.scheduled();
        verify(db, times(2)).getNextBindingRequiringProjectionForUpdateSkipLocked(List.of());
        verify(db).getNextNoticeForUpdateSkipLocked(List.of());
    }

    @Test
    void overlappingScheduledCallbacksDoNotDuplicateTheScan() throws Exception {
        var delivery = delivery(true);
        var entered = new CountDownLatch(1);
        var release = new CountDownLatch(1);
        when(db.getNextBindingRequiringProjectionForUpdateSkipLocked(anyList()))
                .thenAnswer(
                        ignored -> {
                            entered.countDown();
                            assertTrue(release.await(10, TimeUnit.SECONDS));
                            return null;
                        });
        try (var pool = Executors.newVirtualThreadPerTaskExecutor()) {
            var first = pool.submit(delivery::scheduled);
            try {
                assertTrue(entered.await(10, TimeUnit.SECONDS));
                delivery.scheduled();
                verify(db).getNextBindingRequiringProjectionForUpdateSkipLocked(List.of());
            } finally {
                release.countDown();
            }
            first.get(10, TimeUnit.SECONDS);
        }
        verify(db).getNextNoticeForUpdateSkipLocked(List.of());
    }

    @Test
    void explicitMaintenancePollingBypassesIdleWithoutChangingUncertainRecovery() {
        var delivery = delivery(true);
        delivery.scheduled();
        delivery.poll();
        verify(db, times(2)).getNextBindingRequiringProjectionForUpdateSkipLocked(List.of());
        verify(db, times(2)).getNextNoticeForUpdateSkipLocked(List.of());
        verify(db).updateExpiredSendingNoticesUnknown();
        verify(db, never()).updatePendingNoticeSending(anyString());
    }

    @Test
    void allLockedNoticeRowsExcludeOnlyTheirBindingWithoutInventingVersionZero() {
        var delivery = delivery(true);
        var busy = scheduled(binding("busy-binding", false), null);
        assertNull(busy.notice());
        assertNull(busy.noticeRevision());
        assertNull(busy.noticeBindingRevision());
        when(db.getNextNoticeForUpdateSkipLocked(List.of())).thenReturn(busy);
        delivery.scheduled();
        verify(db).getNextNoticeForUpdateSkipLocked(List.of("busy-binding"));
        verify(db, never()).getNextPendingNotice();
        verify(db, never()).updatePendingNoticeSending(anyString());
        verify(db, never()).updateNoticeOutcome(anyString(), anyString(), anyString());
        verifyNoInteractions(tasks, interactions, channel);
    }

    @Test
    void incompleteNonNullNoticeFailsBeforeAnyTaskReadOrSideEffect() {
        var b = binding("binding", false);
        var malformed =
                new FeishuMapper.ScheduledNoticeRow(
                        b.id(),
                        b.tenantKey(),
                        b.openId(),
                        b.chatId(),
                        b.tenant(),
                        b.subject(),
                        b.conversationId(),
                        b.revision(),
                        b.muted(),
                        b.activeTurn(),
                        "notice",
                        "turn",
                        "binding",
                        null,
                        2L,
                        "payload",
                        "pending",
                        null);
        when(db.getNextNoticeForUpdateSkipLocked(anyList())).thenReturn(malformed);
        assertThrows(NullPointerException.class, delivery(true)::scheduled);
        verify(db, never()).updatePendingNoticeSending(anyString());
        verify(db, never()).updateNoticeOutcome(anyString(), anyString(), anyString());
        verifyNoInteractions(tasks, interactions, channel);
    }

    @Test
    void busyBindingExclusionAndProjectionAttemptsBothHaveATwentyItemBound() {
        var projected = new AtomicInteger();
        when(db.getNextBindingRequiringProjectionForUpdateSkipLocked(anyList()))
                .thenAnswer(
                        call -> {
                            assertEquals(projected.get(), call.<List<String>>getArgument(0).size());
                            return binding("projected-" + projected.incrementAndGet(), true);
                        });
        var claimed = new AtomicInteger();
        when(db.getNextNoticeForUpdateSkipLocked(anyList()))
                .thenAnswer(
                        call -> {
                            assertEquals(claimed.get(), call.<List<String>>getArgument(0).size());
                            return scheduled(
                                    binding("busy-" + claimed.incrementAndGet(), false), null);
                        });
        delivery(true).scheduled();
        assertEquals(20, projected.get());
        assertEquals(20, claimed.get());
        verify(db, never()).insertOrReactivateNotice(any());
        verify(db, never()).updatePendingNoticeSending(anyString());
        verifyNoInteractions(tasks, interactions, channel);
    }

    private static Binding binding(String id, boolean muted) {
        return new Binding(
                id,
                "channel-tenant",
                "user",
                "chat",
                "tenant",
                "subject",
                "conversation",
                3,
                muted,
                "turn");
    }

    private static FeishuMapper.ScheduledNoticeRow scheduled(Binding b, Notice n) {
        return new FeishuMapper.ScheduledNoticeRow(
                b.id(),
                b.tenantKey(),
                b.openId(),
                b.chatId(),
                b.tenant(),
                b.subject(),
                b.conversationId(),
                b.revision(),
                b.muted(),
                b.activeTurn(),
                n == null ? null : n.id(),
                n == null ? null : n.turnId(),
                n == null ? null : n.bindingId(),
                n == null ? null : n.bindingRevision(),
                n == null ? null : n.revision(),
                n == null ? null : n.payload(),
                n == null ? null : n.status(),
                n == null ? null : n.reason());
    }
}
