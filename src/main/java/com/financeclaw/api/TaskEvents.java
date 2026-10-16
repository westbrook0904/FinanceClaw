package com.financeclaw.api;

import com.financeclaw.app.task.TaskQueryService;
import com.financeclaw.domain.Task.*;
import java.time.Duration;
import java.util.concurrent.Semaphore;
import java.util.concurrent.atomic.AtomicLong;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.codec.ServerSentEvent;
import org.springframework.stereotype.Component;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;
import reactor.core.scheduler.Schedulers;

/** Spring MVC adapts this Publisher to SSE; Reactor owns scheduling, cancellation and timeouts. */
@Component
public class TaskEvents {
  private final TaskQueryService tasks;
  private final Duration timeout, poll;
  private final Semaphore slots;

  public TaskEvents(
      TaskQueryService tasks,
      @Value("${financeclaw.sse.max-connections:32}") int max,
      @Value("${financeclaw.sse.timeout-ms:30000}") long timeout,
      @Value("${financeclaw.sse.poll-ms:250}") long poll) {
    if (max < 1 || max > 128 || timeout < 100 || poll < 10)
      throw new IllegalArgumentException("Invalid SSE limits");
    this.tasks = tasks;
    this.timeout = Duration.ofMillis(timeout);
    this.poll = Duration.ofMillis(poll);
    this.slots = new Semaphore(max);
  }

  public Flux<ServerSentEvent<Event>> open(
      Owner owner, String conversation, String turn, long after) {
    tasks.events(owner, conversation, turn, after);
    // Subscription ownership is separate from worker ownership: HTTP cancellation cannot stop a
    // task.
    return Flux.using(
        () -> {
          if (!slots.tryAcquire()) throw new Problem(429, "too_many_streams");
          return new AtomicLong(after);
        },
        cursor ->
            Flux.interval(Duration.ZERO, poll)
                .onBackpressureLatest()
                .concatMap(
                    tick ->
                        Mono.fromCallable(
                                () -> {
                                  var events =
                                      tasks.events(owner, conversation, turn, cursor.get());
                                  if (!events.isEmpty()) cursor.set(events.getLast().revision());
                                  Turn snapshot = tasks.get(owner, conversation, turn);
                                  return new Batch(
                                      events,
                                      snapshot.terminal() && cursor.get() >= snapshot.revision());
                                })
                            .subscribeOn(Schedulers.boundedElastic()),
                    1)
                .takeUntil(Batch::complete)
                .concatMapIterable(
                    batch ->
                        batch.events().isEmpty()
                            ? java.util.List.of(
                                ServerSentEvent.<Event>builder().comment("keepalive").build())
                            : batch.events().stream()
                                .map(
                                    event ->
                                        ServerSentEvent.builder(event)
                                            .id(Long.toString(event.revision()))
                                            .event(event.kind())
                                            .build())
                                .toList())
                .take(timeout),
        ignored -> slots.release());
  }

  private record Batch(java.util.List<Event> events, boolean complete) {}
}
