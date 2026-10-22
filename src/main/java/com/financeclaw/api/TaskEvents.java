package com.financeclaw.api;

import com.financeclaw.app.task.TaskEventHub;
import com.financeclaw.app.task.TaskQueryService;
import com.financeclaw.domain.Task.*;
import java.time.Duration;
import java.util.List;
import java.util.Set;
import java.util.concurrent.Semaphore;
import java.util.concurrent.atomic.AtomicLong;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.codec.ServerSentEvent;
import org.springframework.stereotype.Component;
import reactor.core.publisher.Flux;
import reactor.core.scheduler.Schedulers;

/** One durable replay followed by worker-owned AgentScope events; idle SSE never queries SQL. */
@Component
public class TaskEvents {
  private final TaskQueryService tasks;
  private final TaskEventHub events;
  private final Duration timeout, heartbeat;
  private final Semaphore slots;
  private final int capacity;
  private final String role;

  public TaskEvents(
      TaskQueryService tasks,
      TaskEventHub events,
      @Value("${financeclaw.sse.max-connections:32}") int max,
      @Value("${financeclaw.sse.timeout-ms:30000}") long timeout,
      @Value("${financeclaw.sse.heartbeat-ms:15000}") long heartbeat,
      @Value("${financeclaw.sse.buffer-capacity:256}") int capacity,
      @Value("${financeclaw.deployment.role:standalone}") String role) {
    if (max < 1 || max > 128 || timeout < 100 || heartbeat < 100 || capacity < 1 || capacity > 4096)
      throw new IllegalArgumentException("Invalid SSE limits");
    this.tasks = tasks;
    this.events = events;
    this.timeout = Duration.ofMillis(timeout);
    this.heartbeat = Duration.ofMillis(heartbeat);
    this.slots = new Semaphore(max);
    this.capacity = capacity;
    this.role = role;
  }

  public Flux<ServerSentEvent<Event>> open(
      Owner owner, String conversation, String turn, long after) {
    // HTTP validation happens before response headers are committed.
    Turn snapshot = tasks.get(owner, conversation, turn);
    if (after < 0 || after > snapshot.revision()) throw new Problem(422, "invalid_event_cursor");
    if (!role.equals("standalone")) throw new Problem(503, "sse_requires_standalone");
    if (snapshot.terminal() && after == snapshot.revision()) return Flux.empty();
    return Flux.using(
        () -> {
          if (!slots.tryAcquire()) throw new Problem(429, "too_many_streams");
          return events.subscribe(snapshot, capacity);
        },
        subscription -> {
          var cursor = new AtomicLong(after);
          // Subscribe before replay: events committed in the HTTP/subscription gap are in SQL;
          // events committed during replay are buffered, then deduplicated by durable revision.
          var updates =
              Flux.concat(replay(snapshot, after), subscription.events())
                  .concatMap(
                      event -> {
                        if (event.revision() > 0 && event.revision() > cursor.get() + 1)
                          // Commit callbacks may arrive out of order. Repair only on a detected
                          // gap.
                          return Flux.concat(replay(snapshot, cursor.get()), Flux.just(event));
                        return Flux.just(event);
                      },
                      1)
                  .filter(event -> event.revision() == 0 || event.revision() > cursor.get())
                  .doOnNext(
                      event -> {
                        if (event.revision() > 0) cursor.set(event.revision());
                      })
                  .map(
                      event -> {
                        var frame = ServerSentEvent.builder(event).event(event.kind());
                        if (event.revision() > 0) frame.id(Long.toString(event.revision()));
                        return frame.build();
                      });
          return Flux.merge(
                  1,
                  updates,
                  Flux.interval(heartbeat)
                      .onBackpressureDrop()
                      .map(tick -> ServerSentEvent.<Event>builder().comment("keepalive").build()))
              .takeUntil(
                  frame ->
                      frame.data() != null
                          && frame.data().kind().equals("turn")
                          && Set.of("completed", "failed", "cancelled")
                              .contains(frame.data().status()))
              .take(timeout);
        },
        subscription -> {
          subscription.close();
          slots.release();
        });
  }

  private Flux<Event> replay(Turn snapshot, long after) {
    return Flux.<List<Event>, Long>generate(
            () -> after,
            (cursor, sink) -> {
              var page = tasks.replay(snapshot, cursor);
              if (page.isEmpty()) sink.complete();
              else sink.next(page);
              return page.isEmpty() ? cursor : page.getLast().revision();
            })
        .takeUntil(page -> page.size() < 100)
        .concatMapIterable(page -> page, 1)
        .subscribeOn(Schedulers.boundedElastic());
  }
}
