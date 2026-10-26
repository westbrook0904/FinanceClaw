package com.financeclaw.infrastructure.runtime;

import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.Task.*;
import io.micrometer.observation.Observation;
import io.micrometer.observation.ObservationRegistry;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.function.Supplier;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;
import reactor.util.context.ContextView;

/**
 * 显式attempt关联加Reactor Context父句柄，不依赖跨线程ThreadLocal。
 * ID仅用于进程内在途查找，Observation只含固定kind/outcome；不传正文或原始异常。
 */
public final class RuntimeObservations {
    private static final Object PARENT = new Object();
    private static final RuntimeObservations DISABLED =
            new RuntimeObservations(ObservationRegistry.NOOP);
    private final ObservationRegistry registry;
    private final Map<String, Root> active = new ConcurrentHashMap<>();

    public RuntimeObservations(ObservationRegistry registry) {
        this.registry = java.util.Objects.requireNonNull(registry);
    }

    public static RuntimeObservations disabled() {
        return DISABLED;
    }

    public <T> T attempt(Claim claim, Supplier<T> work) {
        if (registry == ObservationRegistry.NOOP) return work.get();
        String key = claim.command().attemptId();
        Root root = new Root(start("attempt", null));
        Root existing = active.putIfAbsent(key, root);
        // 重入观测不能改变执行权威或替换另一调用的父句柄。
        if (existing != null) {
            root.finish("cancelled");
            return work.get();
        }
        String outcome = "error";
        try {
            T result = work.get();
            outcome = "returned";
            return result;
        } catch (RuntimeException failure) {
            outcome = outcome(failure);
            throw failure;
        } finally {
            active.remove(key, root);
            root.finish(outcome);
        }
    }

    public <T> Flux<T> model(Claim claim, Supplier<Flux<T>> work) {
        return Flux.deferContextual(
                context -> {
                    Span span = child(claim, "model", context);
                    Flux<T> source;
                    try {
                        source = java.util.Objects.requireNonNull(work.get());
                    } catch (Throwable failure) {
                        span.finish(outcome(failure));
                        return Flux.error(failure);
                    }
                    return source.doOnComplete(() -> span.finish("returned"))
                            .doOnError(failure -> span.finish(outcome(failure)))
                            .doOnCancel(() -> span.finish("cancelled"))
                            .doFinally(signal -> span.finish("cancelled"))
                            .contextWrite(c -> span.root == null ? c : c.put(PARENT, span));
                });
    }

    public <T> Mono<T> tool(Claim claim, Supplier<Mono<T>> work) {
        return Mono.deferContextual(
                context -> {
                    Span span = child(claim, "tool", context);
                    Mono<T> source;
                    try {
                        source = java.util.Objects.requireNonNull(work.get());
                    } catch (Throwable failure) {
                        span.finish(outcome(failure));
                        return Mono.error(failure);
                    }
                    return source.doOnSuccess(ignored -> span.finish("returned"))
                            .doOnError(failure -> span.finish(outcome(failure)))
                            .doOnCancel(() -> span.finish("cancelled"))
                            .doFinally(signal -> span.finish("cancelled"))
                            .contextWrite(c -> span.root == null ? c : c.put(PARENT, span));
                });
    }

    private Span child(Claim claim, String kind, ContextView context) {
        Root root = active.get(claim.command().attemptId());
        if (root == null) return new Span(null, Observation.NOOP);
        Span inherited = context.getOrDefault(PARENT, null);
        if (inherited != null && inherited.root != root) return new Span(null, Observation.NOOP);
        synchronized (root) {
            if (root.closed.get()) return new Span(null, Observation.NOOP);
            Observation parent =
                    inherited != null && inherited.root == root && !inherited.closed.get()
                            ? inherited.observation
                            : root.observation;
            Span span = new Span(root, start(kind, parent));
            root.children.add(span);
            return span;
        }
    }

    private Observation start(String kind, Observation parent) {
        Observation observation = Observation.NOOP;
        try {
            observation =
                    Observation.createNotStarted("financeclaw.runtime.call", registry)
                            .parentObservation(parent)
                            .lowCardinalityKeyValue("kind", kind);
            observation.start();
            return observation;
        } catch (Throwable failure) {
            // Handler failure must not suppress, duplicate or replace the actual call.
            stop(observation, "error");
            return Observation.NOOP;
        }
    }

    private static void stop(Observation observation, String outcome) {
        try {
            observation.lowCardinalityKeyValue("outcome", outcome);
        } catch (Throwable ignored) {
            /* telemetry cannot change business outcome */
        }
        try {
            observation.stop();
        } catch (Throwable ignored) {
            /* no retry: a handler may already have run */
        }
    }

    private static String outcome(Throwable failure) {
        if (failure instanceof Blocked) return "blocked";
        if (failure instanceof Fenced) return "fenced";
        return "error";
    }

    private static final class Root {
        final Observation observation;
        final Set<Span> children = ConcurrentHashMap.newKeySet();
        final AtomicBoolean closed = new AtomicBoolean();

        Root(Observation observation) {
            this.observation = observation;
        }

        synchronized void finish(String outcome) {
            if (!closed.compareAndSet(false, true)) return;
            for (Span child : Set.copyOf(children)) child.finish("cancelled");
            children.clear();
            stop(observation, outcome);
        }
    }

    private static final class Span {
        final Root root;
        final Observation observation;
        final AtomicBoolean closed = new AtomicBoolean();

        Span(Root root, Observation observation) {
            this.root = root;
            this.observation = observation;
        }

        void finish(String outcome) {
            if (!closed.compareAndSet(false, true)) return;
            try {
                stop(observation, outcome);
            } finally {
                if (root != null) root.children.remove(this);
            }
        }
    }

    /** Package-visible diagnostics prove completed attempts leave no retained correlation. */
    int activeAttempts() {
        return active.size();
    }
}
