package com.financeclaw.app.task;

import com.financeclaw.domain.Task.*;
import java.util.*;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.function.Predicate;
import org.springframework.stereotype.Component;
import org.springframework.transaction.support.TransactionSynchronization;
import org.springframework.transaction.support.TransactionSynchronizationManager;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Sinks;
import reactor.core.scheduler.Schedulers;

/** Bounded, process-local observers of the single worker-owned AgentScope execution. */
@Component
public final class TaskEventHub {
  private static final Set<String> PUBLIC_PROGRESS =
      Set.of("text_delta", "tool_started", "tool_completed", "tool_failed");
  private final Map<String, Set<Subscription>> observers = new HashMap<>();
  private final Map<String, Progress> attempts = new HashMap<>();

  /** The caller supplies the owner-checked Turn; no client-selected identity is trusted here. */
  public synchronized Subscription subscribe(Turn turn, int capacity) {
    if (capacity < 1 || capacity > 4096) throw new IllegalArgumentException("Invalid SSE buffer");
    var subscription = new Subscription(turn, capacity);
    observers.computeIfAbsent(turn.id(), ignored -> new HashSet<>()).add(subscription);
    return subscription;
  }

  /**
   * Register before checking dispatch policy so concurrent invalidation cannot miss this attempt.
   */
  public synchronized Progress begin(Claim claim) {
    if (!claim.command().turnId().equals(claim.turn().id())
        || attempts.containsKey(claim.command().attemptId())
        || attempts.values().stream()
            .anyMatch(
                existing ->
                    existing.claim.turn().id().equals(claim.turn().id())
                        && existing.claim.turn().revision() >= claim.turn().revision()))
      throw new Fenced();
    // An old executor may still be unwinding when recovery or a resume starts another attempt.
    stop(p -> p.claim.turn().id().equals(claim.turn().id()));
    var progress = new Progress(claim);
    attempts.put(claim.command().attemptId(), progress);
    return progress;
  }

  public synchronized void progress(Claim claim, String kind, String text, String tool) {
    if (!PUBLIC_PROGRESS.contains(kind)) throw new IllegalArgumentException("Unknown public event");
    var progress = attempts.get(claim.command().attemptId());
    if (progress == null || progress.closed || !progress.claim.equals(claim)) throw new Fenced();
    // No IDs on transient events: reconnect cursors refer only to committed business revisions.
    if (text == null || text.length() <= 4096) {
      emit(new Event(claim.turn().id(), 0, "running", null, kind, text, tool), progress);
    } else {
      for (int start = 0; start < text.length(); ) {
        int end = Math.min(start + 4096, text.length());
        if (end < text.length() && Character.isHighSurrogate(text.charAt(end - 1))) end--;
        emit(
            new Event(
                claim.turn().id(), 0, "running", null, kind, text.substring(start, end), tool),
            progress);
        start = end;
      }
    }
  }

  /** The database transaction remains authoritative; rolled-back events never reach observers. */
  public void committed(Event event) {
    afterCommit(() -> publish(event));
  }

  private synchronized void publish(Event event) {
    if (!event.status().equals("running"))
      stop(
          p ->
              p.claim.turn().id().equals(event.turnId())
                  && event.revision() >= p.claim.turn().revision());
    emit(event, null);
  }

  public void invalidateConversation(String conversation) {
    invalidate(t -> t.conversationId().equals(conversation));
  }

  public void invalidateOwner(Owner owner) {
    invalidate(t -> t.tenant().equals(owner.tenant()) && t.subject().equals(owner.subject()));
  }

  private void invalidate(Predicate<Turn> match) {
    List<Progress> affectedAttempts;
    List<Subscription> affectedObservers;
    synchronized (this) {
      // Capture while the caller still holds the grant/privacy database lock. A newer, authorized
      // turn can begin after commit but before this callback runs and must not be invalidated.
      affectedAttempts =
          attempts.values().stream().filter(p -> match.test(p.claim.turn())).toList();
      affectedObservers =
          observers.values().stream()
              .flatMap(Set::stream)
              .filter(subscription -> match.test(subscription.turn))
              .toList();
    }
    afterCommit(
        () -> {
          synchronized (this) {
            for (var progress : affectedAttempts) {
              progress.invalidated = true;
              progress.close();
            }
            // Closed attempts can still have normal tail tokens buffered for slow consumers.
            for (var subscription : affectedObservers) subscription.generation++;
          }
        });
  }

  private void stop(Predicate<Progress> match) {
    for (var progress : List.copyOf(attempts.values())) {
      if (match.test(progress)) {
        progress.invalidated = true;
        progress.close();
      }
    }
  }

  private void emit(Event event, Progress progress) {
    for (var subscription : List.copyOf(observers.getOrDefault(event.turnId(), Set.of()))) {
      if (progress != null && !sameIdentity(subscription.turn, progress.claim.turn())) continue;
      var result =
          subscription.sink.tryEmitNext(new Delivery(event, progress, subscription.generation));
      if (result.isFailure()) subscription.overflow();
    }
  }

  private static boolean sameIdentity(Turn left, Turn right) {
    return left.id().equals(right.id())
        && left.conversationId().equals(right.conversationId())
        && left.tenant().equals(right.tenant())
        && left.subject().equals(right.subject());
  }

  private static void afterCommit(Runnable action) {
    if (!TransactionSynchronizationManager.isActualTransactionActive()) {
      action.run();
      return;
    }
    TransactionSynchronizationManager.registerSynchronization(
        new TransactionSynchronization() {
          @Override
          public void afterCommit() {
            action.run();
          }
        });
  }

  private record Delivery(Event event, Progress progress, long generation) {}

  public final class Progress implements AutoCloseable {
    private final Claim claim;
    private boolean closed;
    private volatile boolean invalidated;

    private Progress(Claim claim) {
      this.claim = claim;
    }

    /**
     * Normal SDK completion preserves already-published tail tokens until the observer consumes.
     */
    @Override
    public void close() {
      synchronized (TaskEventHub.this) {
        closed = true;
        attempts.remove(claim.command().attemptId(), this);
      }
    }
  }

  public final class Subscription implements AutoCloseable {
    private final Turn turn;
    private final ArrayBlockingQueue<Delivery> queue;
    private final Sinks.Many<Delivery> sink;
    private volatile long generation;
    private volatile boolean closed;

    private Subscription(Turn turn, int capacity) {
      this.turn = Objects.requireNonNull(turn);
      queue = new ArrayBlockingQueue<>(capacity);
      sink = Sinks.many().unicast().onBackpressureBuffer(queue);
    }

    public Flux<Event> events() {
      return sink.asFlux()
          // Never invoke a browser's subscriber on the execution or transaction-commit thread.
          .publishOn(Schedulers.boundedElastic(), 1)
          .filter(
              delivery ->
                  !closed
                      && (delivery.progress() == null
                          || (!delivery.progress().invalidated
                              && delivery.generation() == generation)))
          .map(Delivery::event)
          .doFinally(ignored -> close());
    }

    private void detach() {
      closed = true;
      var existing = observers.get(turn.id());
      if (existing != null) {
        existing.remove(this);
        if (existing.isEmpty()) observers.remove(turn.id());
      }
    }

    private void overflow() {
      detach();
      queue.clear();
      // This affects only this observer. Durable events can be recovered using Last-Event-ID.
      sink.tryEmitError(new IllegalStateException("sse_consumer_too_slow"));
    }

    @Override
    public void close() {
      synchronized (TaskEventHub.this) {
        if (closed) return;
        detach();
        queue.clear();
        sink.tryEmitComplete();
      }
    }
  }
}
