package com.financeclaw.infrastructure.runtime;

import static org.assertj.core.api.Assertions.*;

import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.Task.*;
import io.micrometer.observation.*;
import java.time.Duration;
import java.util.*;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.atomic.AtomicInteger;
import org.junit.jupiter.api.Test;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;
import reactor.core.scheduler.Schedulers;

class RuntimeObservationsTest {
    private final Claim claim =
            new Claim(
                    new Command(
                            "secret-command", "turn", "running", "secret-attempt", 1, true, null),
                    new Turn(
                            "turn",
                            "secret-conversation",
                            "secret-tenant",
                            "secret-user",
                            "key",
                            "hash",
                            "private-prompt",
                            "release",
                            "running",
                            2,
                            null,
                            null));

    private static final class Recording implements ObservationHandler<Observation.Context> {
        final List<Observation.Context> stopped = new CopyOnWriteArrayList<>();

        @Override
        public boolean supportsContext(Observation.Context context) {
            return true;
        }

        @Override
        public void onStop(Observation.Context context) {
            stopped.add(context);
        }
    }

    @Test
    void explicitParentsSurviveReactorThreadChangesWithoutIdentityOrErrorPayloads() {
        var registry = ObservationRegistry.create();
        var recording = new Recording();
        registry.observationConfig().observationHandler(recording);
        var observations = new RuntimeObservations(registry);
        String result =
                observations.attempt(
                        claim,
                        () ->
                                observations
                                        .tool(
                                                claim,
                                                () ->
                                                        observations
                                                                .model(
                                                                        claim,
                                                                        () ->
                                                                                Flux.just(
                                                                                                "actual-result")
                                                                                        .publishOn(
                                                                                                Schedulers
                                                                                                        .parallel()))
                                                                .single())
                                        .subscribeOn(Schedulers.boundedElastic())
                                        .block(Duration.ofSeconds(3)));
        assertThat(result).isEqualTo("actual-result");
        assertThat(observations.activeAttempts()).isZero();
        assertThat(registry.getCurrentObservation()).isNull();
        assertThat(recording.stopped).hasSize(3);
        var root = kind(recording, "attempt");
        var tool = kind(recording, "tool");
        var model = kind(recording, "model");
        assertThat(root.getParentObservation()).isNull();
        assertThat(tool.getParentObservation().getContextView()).isSameAs(root);
        assertThat(model.getParentObservation().getContextView()).isSameAs(tool);
        for (var context : recording.stopped) {
            assertThat(context.getLowCardinalityKeyValues())
                    .extracting(v -> v.getKey())
                    .containsExactlyInAnyOrder("kind", "outcome");
            assertThat(context.getLowCardinalityKeyValue("outcome").getValue())
                    .isEqualTo("returned");
            assertThat(context.getHighCardinalityKeyValues()).isEmpty();
            assertThat(context.getError()).isNull();
            assertThat(context.getContextualName()).isNull();
        }
    }

    @Test
    void errorsAndCancellationStopOnceAndRootCompletionDropsCorrelation() {
        var registry = ObservationRegistry.create();
        var recording = new Recording();
        registry.observationConfig().observationHandler(recording);
        var observations = new RuntimeObservations(registry);
        assertThatThrownBy(
                        () ->
                                observations.attempt(
                                        claim,
                                        () ->
                                                observations
                                                        .model(
                                                                claim,
                                                                () ->
                                                                        Flux.error(
                                                                                new Blocked(
                                                                                        "private-body")))
                                                        .blockLast()))
                .hasMessage("private-body");
        assertThat(recording.stopped)
                .hasSize(2)
                .allSatisfy(
                        context -> {
                            assertThat(context.getLowCardinalityKeyValue("outcome").getValue())
                                    .isEqualTo("blocked");
                            assertThat(context.getError()).isNull();
                        });
        recording.stopped.clear();
        var orphan =
                observations.attempt(
                        claim, () -> observations.model(claim, Flux::<String>never).subscribe());
        assertThat(observations.activeAttempts()).isZero();
        assertThat(recording.stopped).hasSize(2);
        assertThat(kind(recording, "model").getLowCardinalityKeyValue("outcome").getValue())
                .isEqualTo("cancelled");
        orphan.dispose();
        assertThat(recording.stopped).hasSize(2);
        observations.model(claim, () -> Flux.just("late")).blockLast();
        assertThat(recording.stopped)
                .hasSize(2); // A late subscription cannot resurrect the old attempt.
    }

    @Test
    void handlerFailuresNeitherSuppressNorRepeatCallsOrReplaceTheirResults() {
        for (boolean failStart : List.of(true, false)) {
            var registry = ObservationRegistry.create();
            registry.observationConfig()
                    .observationHandler(
                            new ObservationHandler<Observation.Context>() {
                                @Override
                                public boolean supportsContext(Observation.Context context) {
                                    return true;
                                }

                                @Override
                                public void onStart(Observation.Context c) {
                                    if (failStart) throw new AssertionError("observer failed");
                                }

                                @Override
                                public void onStop(Observation.Context c) {
                                    if (!failStart)
                                        throw new IllegalStateException("observer failed");
                                }
                            });
            var observations = new RuntimeObservations(registry);
            var actual = new AtomicInteger();
            assertThat(
                            observations.attempt(
                                    claim,
                                    () ->
                                            observations
                                                    .tool(
                                                            claim,
                                                            () ->
                                                                    Mono.fromSupplier(
                                                                            actual
                                                                                    ::incrementAndGet))
                                                    .block()))
                    .isEqualTo(1);
            assertThat(actual).hasValue(1);
            assertThat(observations.activeAttempts()).isZero();
            var expected = new IllegalStateException("actual failure");
            assertThatThrownBy(
                            () ->
                                    observations.attempt(
                                            claim,
                                            () -> {
                                                throw expected;
                                            }))
                    .isSameAs(expected);
            assertThat(observations.activeAttempts()).isZero();
        }
    }

    private Observation.Context kind(Recording recording, String kind) {
        return recording.stopped.stream()
                .filter(c -> c.getLowCardinalityKeyValue("kind").getValue().equals(kind))
                .findFirst()
                .orElseThrow();
    }
}
