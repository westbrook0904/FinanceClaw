package com.financeclaw.app.task;

import com.financeclaw.domain.Task.Problem;
import java.util.HashSet;
import java.util.Set;
import java.util.concurrent.ArrayBlockingQueue;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;
import reactor.core.publisher.Sinks;
import reactor.core.scheduler.Schedulers;

/** 持久事件的无正文唤醒提示；事实和授权仍须从既有业务端口读取。 */
public final class PersistentEventSignals {
    private final Set<Subscription> subscriptions = new HashSet<>();
    private boolean connected;
    private long generation;

    public synchronized boolean isConnected() {
        return connected;
    }

    public synchronized long generation() {
        return generation;
    }

    /** LISTEN 已提交后才允许新流；重连后的全局唤醒覆盖连接间隙。 */
    public synchronized void connected() {
        connected = true;
        wake("*");
    }

    public synchronized void disconnected() {
        connected = false;
        for (var subscription : Set.copyOf(subscriptions)) {
            subscription.detach();
            subscription.disconnected.tryEmitError(
                    new Problem(503, "persistent_events_unavailable"));
            subscription.sink.tryEmitError(new Problem(503, "persistent_events_unavailable"));
        }
    }

    /** 在初始回放之前同步登记，通知与回放交错不会丢失最后一次补读。 */
    public synchronized Subscription subscribe(String turnId) {
        if (!connected) throw new Problem(503, "persistent_events_unavailable");
        var subscription = new Subscription(java.util.Objects.requireNonNull(turnId));
        subscriptions.add(subscription);
        return subscription;
    }

    public synchronized void wake(String turnId) {
        long next = ++generation;
        for (var subscription : Set.copyOf(subscriptions)) {
            if (turnId.equals("*") || turnId.equals(subscription.turnId)) {
                // 满队列中已有一次补读：它会读取最新持久 revision，无须保留每条提示。
                subscription.sink.tryEmitNext(next);
            }
        }
    }

    public final class Subscription implements AutoCloseable {
        private final String turnId;
        private final ArrayBlockingQueue<Long> queue = new ArrayBlockingQueue<>(1);
        private final Sinks.Many<Long> sink = Sinks.many().unicast().onBackpressureBuffer(queue);
        private final Sinks.Empty<Void> disconnected = Sinks.empty();
        private boolean closed;

        private Subscription(String turnId) {
            this.turnId = turnId;
        }

        public Flux<Long> wakeups() {
            // LISTEN 线程不能执行 SQL 回放或等待消费者；队列与预取均为 1。
            return sink.asFlux()
                    .publishOn(Schedulers.boundedElastic(), false, 1)
                    .doFinally(ignored -> close());
        }

        /** 即使初始回放还未结束或下游没有需求，断线也立即终止旧流。 */
        public Mono<Void> disconnected() {
            return disconnected.asMono();
        }

        private void detach() {
            closed = true;
            subscriptions.remove(this);
            queue.clear();
        }

        @Override
        public void close() {
            synchronized (PersistentEventSignals.this) {
                if (closed) return;
                detach();
                // 正常取消/清理不能发布断线完成信号：上游门禁错误取消 wakeups 时，
                // takeUntilOther 若先收到完成，会把待传播的授权错误变成正常结束。
                // 独立断线信号仅由总线 disconnected() 在真正失联时发送。
                sink.tryEmitComplete();
            }
        }
    }
}
