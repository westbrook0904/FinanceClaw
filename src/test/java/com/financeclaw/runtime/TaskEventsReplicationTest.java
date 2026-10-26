package com.financeclaw.runtime;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.Mockito.*;

import com.financeclaw.api.TaskEvents;
import com.financeclaw.app.task.*;
import com.financeclaw.app.task.port.TaskQueryGateway;
import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.Task.*;
import java.time.Duration;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicBoolean;
import org.junit.jupiter.api.Test;
import org.springframework.http.codec.ServerSentEvent;
import reactor.core.publisher.BaseSubscriber;

class TaskEventsReplicationTest {
    private final Owner owner = new Owner("tenant", "alice");
    private final Turn turn =
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
                    null);

    @Test
    void remoteDurableReplayAndLocalDuplicatesDeliverEachRevisionOnce() throws Exception {
        var db = mock(TaskQueryGateway.class);
        var local = new TaskEventHub();
        var remote = new TaskEventHub();
        var rows = new CopyOnWriteArrayList<Event>();
        rows.add(new Event("turn", 1, "running", null));
        when(db.ownedTurn(owner, "conversation", "turn")).thenReturn(turn);
        when(db.events(eq("turn"), anyLong()))
                .thenAnswer(
                        i ->
                                rows.stream()
                                        .filter(e -> e.revision() > i.<Long>getArgument(1))
                                        .limit(100)
                                        .toList());
        var first = new CountDownLatch(1);
        var signals = new PersistentEventSignals();
        signals.connected();
        var streams =
                new TaskEvents(
                        new TaskQueryService(db), local, 1, 5000, 1000, 16, t -> {}, signals);
        var result =
                streams.open(owner, "conversation", "turn", 0)
                        .filter(f -> f.data() != null)
                        .doOnNext(f -> first.countDown())
                        .collectList()
                        .toFuture();
        try {
            assertThat(first.await(2, TimeUnit.SECONDS)).isTrue();
            var second = new Event("turn", 2, "running", null);
            rows.add(second);
            remote.committed(second);
            local.committed(second);
            local.committed(second);
            var terminal = new Event("turn", 3, "completed", null);
            rows.add(terminal);
            remote.committed(terminal);
            signals.wake("turn");
            assertThat(result.get(3, TimeUnit.SECONDS))
                    .extracting(ServerSentEvent::id)
                    .containsExactly("1", "2", "3");
            // Terminal completion releases the single connection permit.
            assertThat(
                            streams.open(owner, "conversation", "turn", 0)
                                    .filter(f -> f.data() != null)
                                    .collectList()
                                    .block(Duration.ofSeconds(3)))
                    .hasSize(3);
        } finally {
            result.cancel(true);
        }
    }

    @Test
    void idleRemoteRevocationClosesStreamAndReleasesPermit() throws Exception {
        var db = mock(TaskQueryGateway.class);
        var allowed = new AtomicBoolean(true);
        when(db.ownedTurn(owner, "conversation", "turn")).thenReturn(turn);
        when(db.events(anyString(), anyLong())).thenReturn(List.of());
        var signals = new PersistentEventSignals();
        signals.connected();
        var streams =
                new TaskEvents(
                        new TaskQueryService(db),
                        new TaskEventHub(),
                        1,
                        5000,
                        30000,
                        16,
                        t -> {
                            if (!allowed.get()) throw new Blocked("agent_access_denied");
                        },
                        signals);
        var future = streams.open(owner, "conversation", "turn", 1).collectList().toFuture();
        allowed.set(false);
        signals.wake("*");
        assertThatThrownBy(() -> future.get(3, TimeUnit.SECONDS))
                .hasRootCauseMessage("agent_access_denied");
        allowed.set(true);
        assertThat(
                        streams.open(owner, "conversation", "turn", 1)
                                .take(Duration.ofMillis(200))
                                .collectList()
                                .block(Duration.ofSeconds(2)))
                .isEmpty();
    }

    @Test
    void queuedDeltaIsCheckedAtDemandAfterRevocationAndNeverDelivered() throws Exception {
        var db = mock(TaskQueryGateway.class);
        var hub = new TaskEventHub();
        var allowed = new AtomicBoolean(true);
        when(db.ownedTurn(owner, "conversation", "turn")).thenReturn(turn);
        when(db.events(eq("turn"), anyLong()))
                .thenAnswer(
                        i ->
                                i.<Long>getArgument(1) == 0
                                        ? List.of(new Event("turn", 1, "running", null))
                                        : List.of());
        var first = new CountDownLatch(1);
        var live = new CountDownLatch(1);
        var stopped = new CountDownLatch(1);
        var checkedWithoutDemand = new CountDownLatch(1);
        var noDemand = new AtomicBoolean();
        var failure = new java.util.concurrent.atomic.AtomicReference<Throwable>();
        var frames = new CopyOnWriteArrayList<Event>();
        var signals = new PersistentEventSignals();
        signals.connected();
        var streams =
                new TaskEvents(
                        new TaskQueryService(db),
                        hub,
                        1,
                        5000,
                        30000,
                        16,
                        t -> {
                            if (noDemand.get()) checkedWithoutDemand.countDown();
                            if (!allowed.get()) throw new Blocked("authorization_changed");
                        },
                        signals);
        var subscriber =
                new BaseSubscriber<ServerSentEvent<Event>>() {
                    @Override
                    protected void hookOnSubscribe(org.reactivestreams.Subscription s) {
                        request(1);
                    }

                    @Override
                    protected void hookOnNext(ServerSentEvent<Event> f) {
                        if (f.data() != null) {
                            frames.add(f.data());
                            first.countDown();
                            if ("public-prefix".equals(f.data().text())) live.countDown();
                        }
                    }

                    @Override
                    protected void hookOnError(Throwable t) {
                        failure.set(t);
                        stopped.countDown();
                    }

                    @Override
                    protected void hookOnComplete() {
                        // 正常清理不得抢先完成并吞掉门禁异常；下方仍要求具体 Blocked。
                        stopped.countDown();
                    }
                };
        streams.open(owner, "conversation", "turn", 0).subscribe(subscriber);
        var claim =
                new Claim(
                        new Command("command", "turn", "running", "attempt", 1, true, null), turn);
        try (var progress = hub.begin(claim)) {
            assertThat(first.await(2, TimeUnit.SECONDS)).isTrue();
            // Confirm the live pipeline has started, independently of the initial replay callback.
            hub.progress(claim, "text_delta", "public-prefix", null);
            subscriber.request(1);
            assertThat(live.await(2, TimeUnit.SECONDS)).isTrue();
            // No downstream demand: the next token may be buffered but may not pass the gate yet.
            noDemand.set(true);
            hub.progress(claim, "text_delta", "private-tail", null);
            assertThat(checkedWithoutDemand.await(200, TimeUnit.MILLISECONDS)).isFalse();
            allowed.set(false);
            noDemand.set(false);
            subscriber.request(10);
            assertThat(stopped.await(3, TimeUnit.SECONDS)).isTrue();
            assertThat(failure.get())
                    .isInstanceOf(Blocked.class)
                    .hasMessage("authorization_changed");
            assertThat(frames).hasSize(2);
            assertThat(frames).extracting(Event::text).containsExactly(null, "public-prefix");
        } finally {
            subscriber.dispose();
        }
    }

    @Test
    void disconnectDuringInitialReplayCancelsTheOldStreamAndReleasesItsPermit() throws Exception {
        var db = mock(TaskQueryGateway.class);
        var signals = new PersistentEventSignals();
        signals.connected();
        when(db.ownedTurn(owner, "conversation", "turn")).thenReturn(turn);
        var entered = new CountDownLatch(1);
        var release = new CountDownLatch(1);
        when(db.events(eq("turn"), anyLong()))
                .thenAnswer(
                        i -> {
                            entered.countDown();
                            release.await(3, TimeUnit.SECONDS);
                            return List.of();
                        });
        var streams =
                new TaskEvents(
                        new TaskQueryService(db),
                        new TaskEventHub(),
                        1,
                        5000,
                        30000,
                        16,
                        t -> {},
                        signals);
        var future = streams.open(owner, "conversation", "turn", 1).collectList().toFuture();
        try {
            assertThat(entered.await(2, TimeUnit.SECONDS)).isTrue();
            signals.disconnected();
            assertThatThrownBy(() -> future.get(2, TimeUnit.SECONDS))
                    .hasRootCauseMessage("persistent_events_unavailable");
        } finally {
            release.countDown();
            future.cancel(true);
        }
        signals.connected();
        assertThat(
                        streams.open(owner, "conversation", "turn", 1)
                                .take(Duration.ofMillis(200))
                                .collectList()
                                .block(Duration.ofSeconds(2)))
                .isEmpty();
    }

    @Test
    void offlineOpenReleasesPermitAndNotificationsBeforeReplayAreCoalesced() {
        var db = mock(TaskQueryGateway.class);
        var signals = new PersistentEventSignals();
        when(db.ownedTurn(owner, "conversation", "turn")).thenReturn(turn);
        when(db.events(anyString(), anyLong())).thenReturn(List.of());
        var streams =
                new TaskEvents(
                        new TaskQueryService(db),
                        new TaskEventHub(),
                        1,
                        5000,
                        30000,
                        16,
                        t -> {},
                        signals);
        assertThatThrownBy(
                        () ->
                                streams.open(owner, "conversation", "turn", 1)
                                        .blockFirst(Duration.ofSeconds(2)))
                .hasMessage("persistent_events_unavailable");
        signals.connected();
        try (var subscription = signals.subscribe("turn")) {
            signals.wake("different-turn");
            for (int i = 0; i < 10000; i++) signals.wake("turn");
            assertThat(
                            subscription
                                    .wakeups()
                                    .take(Duration.ofMillis(200))
                                    .collectList()
                                    .block(Duration.ofSeconds(2)))
                    .hasSize(1);
        }
        assertThat(
                        streams.open(owner, "conversation", "turn", 1)
                                .take(Duration.ofMillis(200))
                                .collectList()
                                .block(Duration.ofSeconds(2)))
                .isEmpty();
    }
}
