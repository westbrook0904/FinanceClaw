package com.financeclaw.api;

import com.financeclaw.app.task.PersistentEventSignals;
import com.financeclaw.app.task.TaskEventHub;
import com.financeclaw.app.task.TaskQueryService;
import com.financeclaw.app.task.port.TaskEventAccess;
import com.financeclaw.domain.Task.*;
import java.time.Duration;
import java.util.List;
import java.util.Set;
import java.util.concurrent.Semaphore;
import java.util.concurrent.atomic.AtomicLong;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.codec.ServerSentEvent;
import org.springframework.stereotype.Component;
import reactor.core.publisher.Flux;
import reactor.core.scheduler.Schedulers;

/**
 * 任务持久事件回放与进程内 AgentScope 事件推送的连接边界。
 *
 * <p>先订阅再回放，按持久修订号去重；数据库提交通知唤醒后补读其他实例提交的事件。 每帧交付前重验当前权限与隐私，临时 token/tool 只在本机传播且没有可回放游标。
 */
@Component
public class TaskEvents {
    private final TaskQueryService tasks;
    private final TaskEventHub events;
    private final Duration timeout, heartbeat;
    private final Semaphore slots;
    private final int capacity;
    private final TaskEventAccess access;
    private final PersistentEventSignals signals;

    /** 显式单机 fixture 入口；生产必须使用带实时门禁与持久补读的装配。 */
    public TaskEvents(
            TaskQueryService tasks,
            TaskEventHub events,
            int max,
            long timeout,
            long heartbeat,
            int capacity) {
        this(tasks, events, max, timeout, heartbeat, capacity, ignored -> {}, null, true);
    }

    @Autowired
    public TaskEvents(
            TaskQueryService tasks,
            TaskEventHub events,
            @Value("${financeclaw.sse.max-connections:32}") int max,
            @Value("${financeclaw.sse.timeout-ms:30000}") long timeout,
            @Value("${financeclaw.sse.heartbeat-ms:15000}") long heartbeat,
            @Value("${financeclaw.sse.buffer-capacity:256}") int capacity,
            TaskEventAccess access,
            PersistentEventSignals signals) {
        this(tasks, events, max, timeout, heartbeat, capacity, access, signals, false);
    }

    private TaskEvents(
            TaskQueryService tasks,
            TaskEventHub events,
            int max,
            long timeout,
            long heartbeat,
            int capacity,
            TaskEventAccess access,
            PersistentEventSignals signals,
            boolean fixture) {
        if (max < 1
                || max > 128
                || timeout < 100
                || heartbeat < 100
                || capacity < 1
                || capacity > 4096) {
            throw new IllegalArgumentException("Invalid SSE limits");
        }
        this.tasks = tasks;
        this.events = events;
        this.timeout = Duration.ofMillis(timeout);
        this.heartbeat = Duration.ofMillis(heartbeat);
        this.slots = new Semaphore(max);
        this.capacity = capacity;
        this.access = java.util.Objects.requireNonNull(access);
        this.signals = fixture ? null : java.util.Objects.requireNonNull(signals);
    }

    public Flux<ServerSentEvent<Event>> open(
            Owner owner, String conversation, String turn, long after) {
        // HTTP validation happens before response headers are committed.
        Turn snapshot = tasks.get(owner, conversation, turn);
        access.check(snapshot);
        if (after < 0 || after > snapshot.revision()) {
            throw new Problem(422, "invalid_event_cursor");
        }
        if (snapshot.terminal() && after == snapshot.revision()) {
            return Flux.empty();
        }
        if (signals != null && !signals.isConnected()) {
            throw new Problem(503, "persistent_events_unavailable");
        }
        return Flux.using(
                () -> {
                    if (!slots.tryAcquire()) {
                        throw new Problem(429, "too_many_streams");
                    }
                    TaskEventHub.Subscription local = null;
                    try {
                        local = events.subscribe(snapshot, capacity);
                        return new Subscriptions(
                                local, signals == null ? null : signals.subscribe(snapshot.id()));
                    } catch (RuntimeException failed) {
                        if (local != null) local.close();
                        slots.release();
                        throw failed;
                    }
                },
                subscription -> {
                    var cursor = new AtomicLong(after);
                    // 已连续排入有界交付管道的高水位，不表示客户端已经收到。
                    // 初始回放/通知补读/缺口修复使用它，避免队列预取领先交付时重复查库。
                    var prepared = new AtomicLong(after);
                    Flux<Event> remote =
                            subscription.remote() == null
                                    ? Flux.never()
                                    : subscription
                                            .remote()
                                            .wakeups()
                                            .concatMap(tick -> replay(snapshot, prepared.get()), 1);
                    // 本机通知先订阅再回放；最终串行交付时才推进游标与复核门禁。
                    // keepalive 也经过门禁，慢客户端不能把预检过的帧跨撤权缓存后再送出。
                    var source =
                            Flux.concat(
                                            replay(snapshot, after),
                                            Flux.merge(1, subscription.local().events(), remote))
                                    .map(TaskEvents::frame);
                    var delivery =
                            Flux.merge(
                                            1,
                                            source,
                                            Flux.interval(heartbeat)
                                                    .onBackpressureDrop()
                                                    .map(
                                                            tick ->
                                                                    ServerSentEvent.<Event>builder()
                                                                            .comment("keepalive")
                                                                            .build()))
                                    .concatMap(
                                            frame -> {
                                                if (frame.data() == null) {
                                                    return Flux.just(frame);
                                                }
                                                Event event = frame.data();
                                                Flux<Event> ordered =
                                                        event.revision() > prepared.get() + 1
                                                                ? Flux.concat(
                                                                        replay(
                                                                                snapshot,
                                                                                prepared.get()),
                                                                        Flux.just(event))
                                                                : Flux.just(event);
                                                return ordered.filter(
                                                                candidate ->
                                                                        candidate.revision() == 0
                                                                                || candidate
                                                                                                .revision()
                                                                                        > prepared
                                                                                                .get())
                                                        .doOnNext(
                                                                candidate -> {
                                                                    if (candidate.revision() > 0)
                                                                        prepared.set(
                                                                                candidate
                                                                                        .revision());
                                                                })
                                                        .map(TaskEvents::frame);
                                            },
                                            0)
                                    // 最后一个异步队列只预取一帧；检查必须在队列之后，按真实
                                    // 下游需求运行，不能在 concatMap 的内层提前检查后缓存帧。
                                    // cursor 仍只在检查通过后推进；断线从客户端 Last-Event-ID 重放。
                                    .publishOn(Schedulers.boundedElastic(), false, 1)
                                    .doOnNext(frame -> access.check(snapshot))
                                    .filter(
                                            frame ->
                                                    frame.data() == null
                                                            || frame.data().revision() == 0
                                                            || frame.data().revision()
                                                                    > cursor.get())
                                    .doOnNext(
                                            frame -> {
                                                if (frame.data() != null
                                                        && frame.data().revision() > 0)
                                                    cursor.set(frame.data().revision());
                                            })
                                    .takeUntil(
                                            frame ->
                                                    frame.data() != null
                                                            && frame.data().kind().equals("turn")
                                                            && Set.of(
                                                                            "completed",
                                                                            "failed",
                                                                            "cancelled")
                                                                    .contains(
                                                                            frame.data().status()))
                                    .take(timeout);
                    return subscription.remote() == null
                            ? delivery
                            : delivery.takeUntilOther(subscription.remote().disconnected());
                },
                subscription -> {
                    subscription.local().close();
                    if (subscription.remote() != null) subscription.remote().close();
                    slots.release();
                });
    }

    private record Subscriptions(
            TaskEventHub.Subscription local, PersistentEventSignals.Subscription remote) {}

    private static ServerSentEvent<Event> frame(Event event) {
        var frame = ServerSentEvent.builder(event).event(event.kind());
        if (event.revision() > 0) frame.id(Long.toString(event.revision()));
        return frame.build();
    }

    private Flux<Event> replay(Turn snapshot, long after) {
        return Flux.<List<Event>, Long>generate(
                        () -> after,
                        (cursor, sink) -> {
                            access.check(snapshot);
                            var page = tasks.replay(snapshot, cursor);
                            if (page.isEmpty()) {
                                sink.complete();
                            } else {
                                sink.next(page);
                            }
                            return page.isEmpty() ? cursor : page.getLast().revision();
                        })
                .takeUntil(page -> page.size() < 100)
                .concatMapIterable(page -> page, 1)
                .subscribeOn(Schedulers.boundedElastic());
    }
}
