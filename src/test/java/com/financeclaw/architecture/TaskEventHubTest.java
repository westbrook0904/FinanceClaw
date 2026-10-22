package com.financeclaw.architecture;

import static org.junit.jupiter.api.Assertions.*;

import com.financeclaw.app.task.TaskEventHub;
import com.financeclaw.domain.Task.*;
import java.time.Duration;
import java.util.List;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicReference;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.reactivestreams.Subscription;
import org.springframework.transaction.support.TransactionSynchronization;
import org.springframework.transaction.support.TransactionSynchronizationManager;
import reactor.core.publisher.BaseSubscriber;

class TaskEventHubTest {
  private final TaskEventHub hub = new TaskEventHub();
  private final Turn turn = turn("turn", "conversation", "alice", 2);
  private final Claim claim = claim(turn, "attempt");

  @Test
  void normalCompletionPreservesBufferedTokensButRejectsLatePublication() {
    try (var observer = hub.subscribe(turn, 8)) {
      try (var execution = hub.begin(claim)) {
        hub.progress(claim, "text_delta", "tail", null);
      }
      assertThrows(Fenced.class, () -> hub.progress(claim, "text_delta", "late", null));
      var received = observer.events().next().block(Duration.ofSeconds(3));
      assertEquals("tail", received.text());
      assertEquals(0, received.revision());
    }
  }

  @Test
  void ownerInvalidationDiscardsBufferedTokensEvenAfterExecutionHasClosed() {
    try (var observer = hub.subscribe(turn, 8)) {
      try (var execution = hub.begin(claim)) {
        hub.progress(claim, "text_delta", "private tail", null);
      }
      hub.invalidateOwner(new Owner("tenant", "alice"));
      var committed = new Event("turn", 3, "blocked", "authorization_changed");
      hub.committed(committed);
      assertEquals(committed, observer.events().next().block(Duration.ofSeconds(3)));
    }
  }

  @Test
  void invalidationAndLiveProgressStayInsideOwnerAndConversationBoundaries() {
    var other = turn("other", "other-conversation", "bob", 2);
    var otherClaim = claim(other, "other-attempt");
    try (var alice = hub.subscribe(turn, 8);
        var bob = hub.subscribe(other, 8);
        var aliceExecution = hub.begin(claim);
        var bobExecution = hub.begin(otherClaim)) {
      hub.progress(claim, "text_delta", "alice private", null);
      hub.progress(otherClaim, "text_delta", "bob private", null);
      hub.invalidateConversation("conversation");
      assertThrows(Fenced.class, () -> hub.progress(claim, "text_delta", "late", null));
      hub.progress(otherClaim, "text_delta", "bob next", null);
      var blocked = new Event("turn", 3, "blocked", "authorization_changed");
      hub.committed(blocked);
      assertEquals(blocked, alice.events().next().block(Duration.ofSeconds(3)));
      assertEquals(
          List.of("bob private", "bob next"),
          bob.events().take(2).map(Event::text).collectList().block(Duration.ofSeconds(3)));
    }
  }

  @ParameterizedTest
  @ValueSource(booleans = {false, true})
  void delayedInvalidationCannotCloseANewlyAuthorizedTurn(boolean ownerScope) {
    var newerTurn = turn("next-turn", "conversation", "alice", 2);
    var newer = claim(newerTurn, "next-attempt");
    try (var oldObserver = hub.subscribe(turn, 8);
        var oldExecution = hub.begin(claim)) {
      hub.progress(claim, "text_delta", "old private text", null);
      TransactionSynchronizationManager.initSynchronization();
      TransactionSynchronizationManager.setActualTransactionActive(true);
      try {
        if (ownerScope) hub.invalidateOwner(new Owner("tenant", "alice"));
        else hub.invalidateConversation("conversation");
        var callbacks = TransactionSynchronizationManager.getSynchronizations();
        // Model the database commit releasing its locks before afterCommit callbacks run.
        TransactionSynchronizationManager.setActualTransactionActive(false);
        try (var newObserver = hub.subscribe(newerTurn, 8);
            var newExecution = hub.begin(newer)) {
          hub.progress(newer, "text_delta", "new authorized text", null);
          callbacks.forEach(TransactionSynchronization::afterCommit);
          assertThrows(Fenced.class, () -> hub.progress(claim, "text_delta", "late", null));
          assertDoesNotThrow(() -> hub.progress(newer, "text_delta", "still authorized", null));
          var blocked = new Event("turn", 3, "blocked", "authorization_changed");
          hub.committed(blocked);
          assertEquals(blocked, oldObserver.events().next().block(Duration.ofSeconds(3)));
          assertEquals(
              List.of("new authorized text", "still authorized"),
              newObserver
                  .events()
                  .take(2)
                  .map(Event::text)
                  .collectList()
                  .block(Duration.ofSeconds(3)));
        }
      } finally {
        TransactionSynchronizationManager.clearSynchronization();
        TransactionSynchronizationManager.setActualTransactionActive(false);
      }
    }
  }

  @Test
  void callerCannotRouteLiveEventsToAnotherOwnerUsingTheSameTurnId() {
    var forged = turn("turn", "conversation", "bob", 2);
    try (var observer = hub.subscribe(forged, 8);
        var execution = hub.begin(claim)) {
      hub.progress(claim, "text_delta", "alice private", null);
      // Durable routing trusts the initial database owner check; it carries no model text.
      var completed = new Event("turn", 3, "completed", null);
      hub.committed(completed);
      assertEquals(completed, observer.events().next().block(Duration.ofSeconds(3)));
    }
  }

  @Test
  void oldCommitCallbacksAndOldHandleCloseCannotFenceANewerAttempt() {
    var old = hub.begin(claim);
    var newerTurn = turn("turn", "conversation", "alice", 6);
    var newer = claim(newerTurn, "resumed");
    try (var observer = hub.subscribe(newerTurn, 8);
        var resumed = hub.begin(newer)) {
      old.close();
      assertThrows(Fenced.class, () -> hub.begin(claim));
      hub.committed(new Event("turn", 3, "waiting", null));
      hub.progress(newer, "text_delta", "new attempt", null);
      assertThrows(Fenced.class, () -> hub.progress(claim, "text_delta", "old attempt", null));
      var received = observer.events().take(2).collectList().block(Duration.ofSeconds(3));
      assertEquals("new attempt", received.getLast().text());
    }
  }

  @Test
  void cancellationInvalidatesAlreadyQueuedLiveEvents() {
    try (var observer = hub.subscribe(turn, 8);
        var execution = hub.begin(claim)) {
      hub.progress(claim, "text_delta", "stale", null);
      var cancelling = new Event("turn", 3, "cancelling", "cancel_requested");
      hub.committed(cancelling);
      assertThrows(Fenced.class, () -> hub.progress(claim, "text_delta", "late", null));
      assertEquals(cancelling, observer.events().next().block(Duration.ofSeconds(3)));
    }
  }

  @Test
  void slowObserverOverflowsWithoutFailingTheProducerOrAnotherObserver() throws Exception {
    var error = new CompletableFuture<Throwable>();
    var slowConsumer =
        new BaseSubscriber<Event>() {
          @Override
          protected void hookOnSubscribe(Subscription subscription) {}

          @Override
          protected void hookOnError(Throwable failure) {
            error.complete(failure);
          }
        };
    try (var slow = hub.subscribe(turn, 2);
        var healthy = hub.subscribe(turn, 32);
        var execution = hub.begin(claim)) {
      slow.events().subscribe(slowConsumer);
      var received = healthy.events().take(8).collectList().toFuture();
      for (int i = 0; i < 8; i++)
        assertDoesNotThrow(() -> hub.progress(claim, "text_delta", "chunk", null));
      assertEquals(8, received.get(3, TimeUnit.SECONDS).size());
      slowConsumer.request(Long.MAX_VALUE);
      assertEquals("sse_consumer_too_slow", error.get(3, TimeUnit.SECONDS).getMessage());
    } finally {
      slowConsumer.cancel();
    }
  }

  @Test
  void subscriberCodeDoesNotRunOnOrHoldTheProducerThread() throws Exception {
    var entered = new CountDownLatch(1);
    var release = new CountDownLatch(1);
    var error = new AtomicReference<Throwable>();
    try (var observer = hub.subscribe(turn, 8);
        var execution = hub.begin(claim);
        var producer = Executors.newSingleThreadExecutor()) {
      var listener =
          observer
              .events()
              .subscribe(
                  event -> {
                    entered.countDown();
                    try {
                      release.await(3, TimeUnit.SECONDS);
                    } catch (InterruptedException interrupted) {
                      Thread.currentThread().interrupt();
                    }
                  },
                  error::set);
      try {
        hub.progress(claim, "text_delta", "first", null);
        assertTrue(entered.await(3, TimeUnit.SECONDS));
        producer
            .submit(() -> hub.progress(claim, "text_delta", "second", null))
            .get(1, TimeUnit.SECONDS);
        assertNull(error.get());
      } finally {
        release.countDown();
        listener.dispose();
      }
    }
  }

  @Test
  void rollbackDoesNotPublishOrInvalidateAndCommitPublishesOnlyAfterCommit() throws Exception {
    try (var observer = hub.subscribe(turn, 8);
        var execution = hub.begin(claim)) {
      TransactionSynchronizationManager.initSynchronization();
      TransactionSynchronizationManager.setActualTransactionActive(true);
      try {
        hub.committed(new Event("turn", 3, "cancelled", "rolled_back"));
        hub.invalidateOwner(new Owner("tenant", "alice"));
        var callbacks = TransactionSynchronizationManager.getSynchronizations();
        assertEquals(2, callbacks.size());
        callbacks.forEach(
            callback -> callback.afterCompletion(TransactionSynchronization.STATUS_ROLLED_BACK));
      } finally {
        TransactionSynchronizationManager.clearSynchronization();
        TransactionSynchronizationManager.setActualTransactionActive(false);
      }
      hub.progress(claim, "text_delta", "still authorized", null);
      var finalEvent = new Event("turn", 4, "completed", null);
      TransactionSynchronizationManager.initSynchronization();
      TransactionSynchronizationManager.setActualTransactionActive(true);
      try {
        hub.committed(finalEvent);
        var received = observer.events().take(2).collectList().toFuture();
        assertFalse(received.isDone());
        // Ordinary completion keeps buffered text; a terminal publication alone fences active work.
        execution.close();
        TransactionSynchronizationManager.getSynchronizations()
            .forEach(TransactionSynchronization::afterCommit);
        var events = received.get(3, TimeUnit.SECONDS);
        assertEquals(
            java.util.Arrays.asList("still authorized", null),
            events.stream().map(Event::text).toList());
        assertEquals(finalEvent, events.getLast());
      } finally {
        TransactionSynchronizationManager.clearSynchronization();
        TransactionSynchronizationManager.setActualTransactionActive(false);
      }
    }
  }

  @Test
  void eventPayloadAndQueuesAreBoundedAndDuplicateExecutionCannotReplaceTheActiveHandle() {
    try (var observer = hub.subscribe(turn, 8);
        var execution = hub.begin(claim)) {
      assertThrows(Fenced.class, () -> hub.begin(claim));
      assertThrows(IllegalArgumentException.class, () -> hub.subscribe(turn, 0));
      assertThrows(IllegalArgumentException.class, () -> hub.subscribe(turn, 4097));
      hub.progress(claim, "text_delta", "x".repeat(9000), null);
      var lengths =
          observer
              .events()
              .take(3)
              .map(event -> event.text().length())
              .collectList()
              .block(Duration.ofSeconds(3));
      assertEquals(List.of(4096, 4096, 808), lengths);
      assertThrows(
          IllegalArgumentException.class, () -> hub.progress(claim, "thinking", "private", null));
    }
  }

  private static Claim claim(Turn turn, String attempt) {
    return new Claim(new Command("command", turn.id(), "running", attempt, 1, true, null), turn);
  }

  private static Turn turn(String id, String conversation, String subject, long revision) {
    return new Turn(
        id,
        conversation,
        "tenant",
        subject,
        "key",
        "hash",
        "message",
        "release",
        "running",
        revision,
        null,
        null);
  }
}
