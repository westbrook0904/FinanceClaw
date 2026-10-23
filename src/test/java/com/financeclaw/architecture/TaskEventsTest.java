package com.financeclaw.architecture;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.Mockito.*;

import com.financeclaw.api.TaskEvents;
import com.financeclaw.app.task.*;
import com.financeclaw.app.task.port.TaskQueryGateway;
import com.financeclaw.domain.Task.*;
import java.time.Duration;
import java.util.*;
import java.util.concurrent.*;
import java.util.stream.LongStream;
import org.junit.jupiter.api.Test;
import org.springframework.http.codec.ServerSentEvent;

class TaskEventsTest {
    private final Owner owner = new Owner("tenant", "alice");
    private final TaskQueryGateway db = mock(TaskQueryGateway.class);
    private final TaskEventHub hub = new TaskEventHub();
    private final TaskQueryService queries = new TaskQueryService(db);

    private Turn turn(String status, long revision) {
        return new Turn(
                "turn",
                "conversation",
                owner.tenant(),
                owner.subject(),
                "key",
                "hash",
                "message",
                "release",
                status,
                revision,
                null,
                null);
    }

    private TaskEvents streams(long timeout) {
        return new TaskEvents(queries, hub, 1, timeout, 100, 256);
    }

    @Test
    void idleConnectionPerformsOnlyAuthorizationAndOneReplayQuery() {
        when(db.ownedTurn(owner, "conversation", "turn")).thenReturn(turn("queued", 1));
        when(db.events("turn", 0)).thenReturn(List.of(new Event("turn", 1, "queued", null)));
        var stream = streams(450);
        var frames =
                stream.open(owner, "conversation", "turn", 0)
                        .collectList()
                        .block(Duration.ofSeconds(3));
        assertThat(frames).anyMatch(f -> "keepalive".equals(f.comment()));
        assertThat(frames).filteredOn(f -> f.data() != null).hasSize(1);
        verify(db).ownedTurn(owner, "conversation", "turn");
        verify(db).events("turn", 0);
        verifyNoMoreInteractions(db);
        // The timeout releases the connection slot.
        assertThat(
                        stream.open(owner, "conversation", "turn", 0)
                                .take(1)
                                .blockLast(Duration.ofSeconds(2)))
                .isNotNull();
    }

    @Test
    void replaysAllPagesAndClosesAtCommittedTerminalWithoutPolling() {
        when(db.ownedTurn(owner, "conversation", "turn")).thenReturn(turn("completed", 202));
        var rows =
                LongStream.rangeClosed(1, 202)
                        .mapToObj(
                                i -> new Event("turn", i, i == 202 ? "completed" : "running", null))
                        .toList();
        when(db.events("turn", 0)).thenReturn(rows.subList(0, 100));
        when(db.events("turn", 100)).thenReturn(rows.subList(100, 200));
        when(db.events("turn", 200)).thenReturn(rows.subList(200, 202));
        var frames =
                streams(5000)
                        .open(owner, "conversation", "turn", 0)
                        .filter(f -> f.data() != null)
                        .collectList()
                        .block(Duration.ofSeconds(2));
        assertThat(frames).hasSize(202);
        assertThat(frames.stream().map(ServerSentEvent::id)).doesNotHaveDuplicates();
        verify(db).ownedTurn(owner, "conversation", "turn");
        verify(db).events("turn", 0);
        verify(db).events("turn", 100);
        verify(db).events("turn", 200);
        verifyNoMoreInteractions(db);
    }

    @Test
    void liveNativeEventsHaveNoCursorAndDoNotTouchDatabase() throws Exception {
        var snapshot = turn("running", 2);
        when(db.ownedTurn(owner, "conversation", "turn")).thenReturn(snapshot);
        var registered = new CountDownLatch(1);
        when(db.events("turn", 2))
                .thenAnswer(
                        i -> {
                            registered.countDown();
                            return List.of();
                        });
        var result =
                streams(5000)
                        .open(owner, "conversation", "turn", 2)
                        .filter(f -> f.data() != null)
                        .collectList()
                        .toFuture();
        assertThat(registered.await(2, TimeUnit.SECONDS)).isTrue();
        var claim =
                new Claim(
                        new Command("command", "turn", "running", "attempt", 1, true, null),
                        snapshot);
        try (var progress = hub.begin(claim)) {
            hub.progress(claim, "text_delta", "hello", null);
        }
        hub.committed(new Event("turn", 3, "completed", null));
        var frames = result.get(2, TimeUnit.SECONDS);
        assertThat(frames).hasSize(2);
        assertThat(frames.getFirst().id()).isNull();
        assertThat(frames.getFirst().data().text()).isEqualTo("hello");
        assertThat(frames.getLast().id()).isEqualTo("3");
        verify(db).ownedTurn(owner, "conversation", "turn");
        verify(db).events("turn", 2);
        verifyNoMoreInteractions(db);
    }

    @Test
    void repairsOutOfOrderCommitGapOnlyWhenSignalled() throws Exception {
        when(db.ownedTurn(owner, "conversation", "turn")).thenReturn(turn("queued", 1));
        var replayed = new CountDownLatch(1);
        when(db.events("turn", 0))
                .thenAnswer(
                        i -> {
                            replayed.countDown();
                            return List.of(new Event("turn", 1, "queued", null));
                        });
        var completed = new Event("turn", 3, "completed", null);
        when(db.events("turn", 1))
                .thenReturn(List.of(new Event("turn", 2, "running", null), completed));
        var result =
                streams(5000)
                        .open(owner, "conversation", "turn", 0)
                        .filter(f -> f.data() != null)
                        .collectList()
                        .toFuture();
        assertThat(replayed.await(2, TimeUnit.SECONDS)).isTrue();
        hub.committed(completed);
        hub.committed(new Event("turn", 2, "running", null));
        assertThat(result.get(2, TimeUnit.SECONDS))
                .extracting(ServerSentEvent::id)
                .containsExactly("1", "2", "3");
        verify(db).events("turn", 1);
    }

    @Test
    void noReplayForAlreadyObservedTerminalAndRejectsWrongOwnerOrCursor() {
        when(db.ownedTurn(owner, "conversation", "turn")).thenReturn(turn("completed", 3));
        assertThat(streams(5000).open(owner, "conversation", "turn", 3).collectList().block())
                .isEmpty();
        verify(db, never()).events(anyString(), anyLong());
        assertThatThrownBy(
                        () ->
                                streams(5000)
                                        .open(
                                                new Owner("tenant", "bob"),
                                                "conversation",
                                                "turn",
                                                0))
                .isInstanceOf(Problem.class)
                .hasMessage("not_found");
        assertThatThrownBy(() -> streams(5000).open(owner, "conversation", "turn", 4))
                .hasMessage("invalid_event_cursor");
    }
}
