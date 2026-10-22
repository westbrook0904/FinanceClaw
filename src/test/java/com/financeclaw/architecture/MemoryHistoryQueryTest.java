package com.financeclaw.architecture;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;

import com.financeclaw.app.memory.MemoryService;
import com.financeclaw.app.memory.port.MemoryRepository;
import com.financeclaw.domain.Memory.Privacy;
import com.financeclaw.domain.Memory.Source;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.Task.Turn;
import com.financeclaw.domain.memory.MemoryPolicy;
import java.util.List;
import java.util.stream.IntStream;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.support.SimpleTransactionStatus;
import org.springframework.transaction.support.TransactionTemplate;

class MemoryHistoryQueryTest {
  private static final Owner OWNER = new Owner("tenant", "alice");
  private final MemoryRepository db = mock(MemoryRepository.class);
  private final PlatformTransactionManager manager = mock(PlatformTransactionManager.class);
  private MemoryService memory;

  @BeforeEach
  void setup() {
    when(manager.getTransaction(any())).thenAnswer(ignored -> new SimpleTransactionStatus());
    when(db.lock(OWNER)).thenReturn(new Privacy("tenant", "alice", 2, false));
    memory =
        new MemoryService(
            db, new TransactionTemplate(manager), new MemoryPolicy(true, false, "embedding-v1"));
  }

  @Test
  void eightHistoryRowsUseOneOwnerLockAndOneBatchReadInOriginalOrder() {
    var history = IntStream.range(0, 8).mapToObj(i -> turn("history-" + i, "alice")).toList();
    var ids = history.stream().map(Turn::id).toList();
    when(db.currentSources(OWNER, 2, ids)).thenReturn(List.of("history-7", "history-1"));

    assertEquals(
        List.of(history.get(1), history.get(7)),
        memory.filterHistory(turn("now", "alice"), history));

    var order = inOrder(db);
    order.verify(db).lock(OWNER);
    order.verify(db).currentSources(OWNER, 2, ids);
    order.verifyNoMoreInteractions();
  }

  @Test
  void anotherOwnersRowsNeverReachTheBatchQuery() {
    var own = turn("own", "alice");
    when(db.currentSources(OWNER, 2, List.of("own"))).thenReturn(List.of("own"));

    assertEquals(
        List.of(own),
        memory.filterHistory(turn("now", "alice"), List.of(own, turn("foreign", "bob"))));

    verify(db).currentSources(OWNER, 2, List.of("own"));
  }

  @Test
  void pendingPurgeCannotExposeHistoryOrReadSources() {
    when(db.lock(OWNER)).thenReturn(new Privacy("tenant", "alice", 2, true));

    assertEquals(
        List.of(), memory.filterHistory(turn("now", "alice"), List.of(turn("old", "alice"))));

    verify(db).lock(OWNER);
    verifyNoMoreInteractions(db);
  }

  @Test
  void privacyIsReadAgainOnTheNextHistoryCallWithoutCrossRequestCaching() {
    var old = turn("old", "alice");
    when(db.currentSources(OWNER, 2, List.of("old"))).thenReturn(List.of("old"));
    assertEquals(List.of(old), memory.filterHistory(turn("now", "alice"), List.of(old)));
    when(db.lock(OWNER)).thenReturn(new Privacy("tenant", "alice", 3, false));

    assertEquals(List.of(), memory.filterHistory(turn("next", "alice"), List.of(old)));

    verify(db, times(2)).lock(OWNER);
    verify(db).currentSources(OWNER, 3, List.of("old"));
  }

  @Test
  void existingOwnerChecksAvoidAnInsertOnEveryModelAndToolCall() {
    when(db.sourceById("now"))
        .thenReturn(new Source("now", "tenant", "alice", 2, "message", "admitted"));

    memory.check(turn("now", "alice"));

    verify(db).lock(OWNER);
    verify(db).sourceById("now");
    verifyNoMoreInteractions(db);
  }

  @Test
  void firstOwnerStillInitializesAndLocksTheAuthoritativeRow() {
    var privacy = new Privacy("tenant", "alice", 1, false);
    when(db.lock(OWNER)).thenReturn(null, privacy);

    assertEquals(privacy, memory.privacy(OWNER));

    var order = inOrder(db);
    order.verify(db).lock(OWNER);
    order.verify(db).ensure(OWNER);
    order.verify(db).lock(OWNER);
    order.verifyNoMoreInteractions();
  }

  @Test
  void emptyOrDisabledHistoryDoesNotAccessDatabase() {
    assertEquals(List.of(), memory.filterHistory(turn("now", "alice"), List.of()));
    var disabled =
        new MemoryService(
            db, new TransactionTemplate(manager), new MemoryPolicy(false, false, "embedding-v1"));
    var history = List.of(turn("old", "alice"));
    assertEquals(history, disabled.filterHistory(turn("now", "alice"), history));
    verifyNoInteractions(db);
  }

  private static Turn turn(String id, String subject) {
    return new Turn(
        id,
        "conversation",
        "tenant",
        subject,
        "key",
        "hash",
        "question",
        "release",
        "completed",
        1,
        "answer",
        null);
  }
}
