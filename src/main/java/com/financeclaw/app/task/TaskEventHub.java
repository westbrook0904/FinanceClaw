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

/** 进程内任务事件总线：按任务、执行尝试和用户权限隔离订阅，事务提交后才发布业务事件；流式增量不通过数据库轮询传播。 */
@Component
public final class TaskEventHub {
    private static final int MAX_SUBSCRIPTION_CAPACITY = 4_096;
    private static final int MAX_PROGRESS_TEXT_CHARS = 4_096;
    private static final Set<String> PUBLIC_PROGRESS =
            Set.of("text_delta", "tool_started", "tool_completed", "tool_failed");
    private final Map<String, Set<Subscription>> observers = new HashMap<>();
    private final Map<String, Progress> attempts = new HashMap<>();

    /** 订阅只接受已经通过归属校验的 Turn，客户端传入的身份不能直接建立订阅。 */
    public synchronized Subscription subscribe(Turn turn, int capacity) {
        if (capacity < 1 || capacity > MAX_SUBSCRIPTION_CAPACITY) {
            throw new IllegalArgumentException("Invalid SSE buffer");
        }
        var subscription = new Subscription(turn, capacity);
        observers.computeIfAbsent(turn.id(), ignored -> new HashSet<>()).add(subscription);
        return subscription;
    }

    /** 在校验执行授权之前登记尝试，避免并发撤权或遗忘漏掉正在开始的执行。 */
    public synchronized Progress begin(Claim claim) {
        if (!claim.command().turnId().equals(claim.turn().id())
                || attempts.containsKey(claim.command().attemptId())
                || attempts.values().stream()
                        .anyMatch(
                                existing ->
                                        existing.claim.turn().id().equals(claim.turn().id())
                                                && existing.claim.turn().revision()
                                                        >= claim.turn().revision())) {
            throw new Fenced();
        }
        // 恢复或续执行开始时，旧执行器可能仍在退出；先使旧尝试失效，禁止其继续发送进度。
        stop(p -> p.claim.turn().id().equals(claim.turn().id()));
        var progress = new Progress(claim);
        attempts.put(claim.command().attemptId(), progress);
        return progress;
    }

    public synchronized void progress(Claim claim, String kind, String text, String tool) {
        if (!PUBLIC_PROGRESS.contains(kind)) {
            throw new IllegalArgumentException("Unknown public event");
        }
        var progress = attempts.get(claim.command().attemptId());
        if (progress == null || progress.closed || !progress.claim.equals(claim)) {
            throw new Fenced();
        }
        // 临时增量没有持久事件编号；断线重连游标只能指向已提交的业务修订号。
        if (text == null || text.length() <= MAX_PROGRESS_TEXT_CHARS) {
            emit(new Event(claim.turn().id(), 0, "running", null, kind, text, tool), progress);
        } else {
            for (int start = 0; start < text.length(); ) {
                int end = Math.min(start + MAX_PROGRESS_TEXT_CHARS, text.length());
                if (end < text.length() && Character.isHighSurrogate(text.charAt(end - 1))) {
                    end--;
                }
                emit(
                        new Event(
                                claim.turn().id(),
                                0,
                                "running",
                                null,
                                kind,
                                text.substring(start, end),
                                tool),
                        progress);
                start = end;
            }
        }
    }

    /** 事件跟随数据库提交；回滚事务绝不向浏览器发布成功状态。 */
    public void committed(Event event) {
        afterCommit(() -> publish(event));
    }

    private synchronized void publish(Event event) {
        if (!event.status().equals("running")) {
            stop(
                    p ->
                            p.claim.turn().id().equals(event.turnId())
                                    && event.revision() >= p.claim.turn().revision());
        }
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
            // 在调用方仍持有授权或隐私锁时捕获旧尝试。提交后才启动的新尝试不属于本次撤权，
            // 即使回调晚于新尝试登记，也不能将它一并失效。
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
                        // Closed attempts can still have normal tail tokens buffered for slow
                        // consumers.
                        for (var subscription : affectedObservers) {
                            subscription.generation++;
                        }
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
            if (progress != null && !sameIdentity(subscription.turn, progress.claim.turn())) {
                continue;
            }
            var result =
                    subscription.sink.tryEmitNext(
                            new Delivery(event, progress, subscription.generation));
            if (result.isFailure()) {
                subscription.overflow();
            }
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
         * Normal SDK completion preserves already-published tail tokens until the observer
         * consumes.
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
                    // 浏览器消费可能缓慢，不能在执行线程或事务提交回调线程上运行订阅者。
                    .publishOn(Schedulers.boundedElastic(), 1)
                    .filter(
                            delivery ->
                                    !closed
                                            && (delivery.progress() == null
                                                    || (!delivery.progress().invalidated
                                                            && delivery.generation()
                                                                    == generation)))
                    .map(Delivery::event)
                    .doFinally(ignored -> close());
        }

        private void detach() {
            closed = true;
            var existing = observers.get(turn.id());
            if (existing != null) {
                existing.remove(this);
                if (existing.isEmpty()) {
                    observers.remove(turn.id());
                }
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
                if (closed) {
                    return;
                }
                detach();
                queue.clear();
                sink.tryEmitComplete();
            }
        }
    }
}
