package com.financeclaw.m0;

import static org.assertj.core.api.Assertions.*;

import com.financeclaw.domain.ExecutionScope;
import com.financeclaw.infrastructure.agentscope.IsolatedAgentFactory;
import com.financeclaw.infrastructure.state.MyBatisAgentStateStore;
import com.financeclaw.infrastructure.state.RuntimeMapper;
import com.financeclaw.m0.support.EffectLedger;
import com.financeclaw.m0.support.ExecutionGate;
import com.financeclaw.m0.support.GuardedAgentRunner;
import io.agentscope.core.message.*;
import io.agentscope.core.model.*;
import io.agentscope.core.state.*;
import io.agentscope.core.tool.*;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;
import org.junit.jupiter.api.Test;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;

class StateAndIsolationIT extends PostgresSupport {
    @Test
    void storeRoundTripsAndRejectsStaleCreateUpdateAndPostDeleteWrites() {
        var scope = scope();
        var first = new MyBatisAgentStateStore(MAPPER, scope);
        var second = new MyBatisAgentStateStore(MAPPER, scope);
        var state =
                AgentState.builder()
                        .sessionId(scope.sessionId())
                        .userId(scope.userId())
                        .addMessage(new UserMessage("user", "中文 state"))
                        .build();
        assertThat(first.supportsVersioning()).isTrue();
        assertThat(
                        first.getVersioned(
                                        scope.userId(),
                                        scope.sessionId(),
                                        "agent_state",
                                        AgentState.class)
                                .version())
                .isZero();
        assertThat(first.saveIfVersion(scope.userId(), scope.sessionId(), "agent_state", state, 0))
                .isEqualTo(1);
        assertThat(
                        second.get(
                                        scope.userId(),
                                        scope.sessionId(),
                                        "agent_state",
                                        AgentState.class)
                                .orElseThrow()
                                .toJson())
                .contains("中文 state");
        assertThat(second.saveIfVersion(scope.userId(), scope.sessionId(), "agent_state", state, 0))
                .isEqualTo(-1);
        assertThat(second.saveIfVersion(scope.userId(), scope.sessionId(), "agent_state", state, 1))
                .isEqualTo(2);
        assertThat(first.saveIfVersion(scope.userId(), scope.sessionId(), "agent_state", state, 1))
                .isEqualTo(-1);
        first.delete(scope.userId(), scope.sessionId());
        assertThat(first.exists(scope.userId(), scope.sessionId())).isFalse();
        assertThat(second.saveIfVersion(scope.userId(), scope.sessionId(), "agent_state", state, 2))
                .isEqualTo(-1);
        assertThat(second.saveIfVersion(scope.userId(), scope.sessionId(), "agent_state", state, 0))
                .isEqualTo(-1);
        assertThat(first.listSessionIds(scope.userId())).isEmpty();
        assertThatThrownBy(
                        () ->
                                new GuardedAgentRunner(DATABASE, scope)
                                        .run(
                                                simultaneousModel(
                                                        new CountDownLatch(0), "must-not-run"),
                                                new Toolkit(),
                                                ProbeFixtures.permissions("none", false),
                                                () -> List.of(new UserMessage("user", "late"))))
                .isInstanceOf(IllegalStateException.class)
                .hasMessageContaining("deleted");
        assertThatThrownBy(
                        () ->
                                first.saveIfVersion(
                                        scope.userId(),
                                        scope.sessionId(),
                                        "agent_state",
                                        state,
                                        -1))
                .isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void stateOwnerAgentReleaseAndConversationAreAllIsolated() {
        var scope = scope();
        var store = new MyBatisAgentStateStore(MAPPER, scope);
        var state = AgentState.builder().sessionId(scope.sessionId()).build();
        store.saveIfVersion(scope.userId(), scope.sessionId(), "agent_state", state, 0);
        var others =
                List.of(
                        new ExecutionScope(
                                "tenant-b",
                                scope.subject(),
                                scope.conversation(),
                                scope.agent(),
                                scope.release()),
                        new ExecutionScope(
                                scope.tenant(),
                                "bob",
                                scope.conversation(),
                                scope.agent(),
                                scope.release()),
                        new ExecutionScope(
                                scope.tenant(),
                                scope.subject(),
                                "conversation-b",
                                scope.agent(),
                                scope.release()),
                        new ExecutionScope(
                                scope.tenant(),
                                scope.subject(),
                                scope.conversation(),
                                "child",
                                scope.release()),
                        new ExecutionScope(
                                scope.tenant(),
                                scope.subject(),
                                scope.conversation(),
                                scope.agent(),
                                "r2"));
        for (var other : others) {
            var otherStore = new MyBatisAgentStateStore(MAPPER, other);
            assertThat(otherStore.exists(other.userId(), other.sessionId())).isFalse();
            assertThatThrownBy(
                            () ->
                                    store.get(
                                            other.userId(),
                                            other.sessionId(),
                                            "agent_state",
                                            AgentState.class))
                    .isInstanceOf(SecurityException.class);
        }
    }

    @Test
    void corruptSavedStateFailsClosedRatherThanStartingANewConversation() {
        var scope = scope();
        DATABASE.getMapper(ProbeMapper.class)
                .insertCorruptState(
                        new RuntimeMapper.StateKey(
                                scope.namespace(),
                                scope.userId(),
                                scope.sessionId(),
                                "agent_state"));
        var store = new MyBatisAgentStateStore(MAPPER, scope);
        assertThatThrownBy(
                        () ->
                                store.getVersioned(
                                        scope.userId(),
                                        scope.sessionId(),
                                        "agent_state",
                                        AgentState.class))
                .isInstanceOf(RuntimeException.class);
    }

    private Model simultaneousModel(CountDownLatch both, String answer) {
        return new Model() {
            public String getModelName() {
                return "m0-barrier";
            }

            public Flux<ChatResponse> stream(
                    List<Msg> messages, List<ToolSchema> tools, GenerateOptions options) {
                return Flux.defer(
                        () -> {
                            both.countDown();
                            try {
                                if (!both.await(8, TimeUnit.SECONDS))
                                    return Flux.error(
                                            new AssertionError("No concurrent execution"));
                            } catch (InterruptedException e) {
                                Thread.currentThread().interrupt();
                                return Flux.error(e);
                            }
                            return Flux.just(
                                    ProbeFixtures.response(
                                            TextBlock.builder().text(answer).build()));
                        });
            }
        };
    }

    @Test
    void twoIndependentSdkInstancesCannotOverwriteTheSameSnapshot() throws Exception {
        var scope = scope();
        var both = new CountDownLatch(2);
        var outcomes = new ArrayList<Future<Object>>();
        try (var executor = Executors.newFixedThreadPool(2)) {
            for (String answer : List.of("answer-a", "answer-b")) {
                outcomes.add(
                        executor.submit(
                                () -> {
                                    try (var agent =
                                            IsolatedAgentFactory.create(
                                                    simultaneousModel(both, answer),
                                                    new Toolkit(),
                                                    new MyBatisAgentStateStore(MAPPER, scope),
                                                    ProbeFixtures.permissions("none", false))) {
                                        return agent.call(
                                                        List.of(new UserMessage("user", answer)),
                                                        IsolatedAgentFactory.context(scope))
                                                .block(Duration.ofSeconds(15));
                                    } catch (RuntimeException error) {
                                        return error;
                                    }
                                }));
            }
            List<Object> values =
                    List.of(
                            outcomes.get(0).get(20, TimeUnit.SECONDS),
                            outcomes.get(1).get(20, TimeUnit.SECONDS));
            assertThat(values.stream().filter(Msg.class::isInstance)).hasSize(1);
            assertThat(
                            values.stream()
                                    .filter(
                                            ConcurrentSessionModificationException.class
                                                    ::isInstance))
                    .hasSize(1);
        }
        var stored =
                new MyBatisAgentStateStore(MAPPER, scope)
                        .getVersioned(
                                scope.userId(), scope.sessionId(), "agent_state", AgentState.class);
        assertThat(stored.version()).isEqualTo(1);
        assertThat(stored.value().getContext())
                .filteredOn(m -> m.getRole() == MsgRole.USER)
                .hasSize(1);
    }

    @Test
    void guardedSameSessionIsRejectedBeforeASecondModelOrToolStarts() throws Exception {
        var scope = scope();
        var entered = new CountDownLatch(1);
        var release = new CountDownLatch(1);
        Model slow =
                new Model() {
                    public String getModelName() {
                        return "slow";
                    }

                    public Flux<ChatResponse> stream(
                            List<Msg> m, List<ToolSchema> t, GenerateOptions o) {
                        return Flux.defer(
                                () -> {
                                    entered.countDown();
                                    try {
                                        if (!release.await(8, TimeUnit.SECONDS))
                                            throw new AssertionError("gate test timed out");
                                    } catch (InterruptedException e) {
                                        Thread.currentThread().interrupt();
                                        return Flux.error(e);
                                    }
                                    return Flux.just(
                                            ProbeFixtures.response(
                                                    TextBlock.builder().text("done").build()));
                                });
                    }
                };
        try (var executor = Executors.newSingleThreadExecutor()) {
            Future<Msg> first =
                    executor.submit(
                            () ->
                                    new GuardedAgentRunner(DATABASE, scope)
                                            .run(
                                                    slow,
                                                    new Toolkit(),
                                                    ProbeFixtures.permissions("none", false),
                                                    () ->
                                                            List.of(
                                                                    new UserMessage(
                                                                            "user", "first"))));
            try {
                assertThat(entered.await(10, TimeUnit.SECONDS)).isTrue();
                assertThatThrownBy(
                                () -> ExecutionGate.acquire(DATABASE.getSqlSessionFactory(), scope))
                        .isInstanceOf(IllegalStateException.class)
                        .hasMessageContaining("already active");
            } finally {
                release.countDown();
            }
            assertThat(first.get(15, TimeUnit.SECONDS).getTextContent()).isEqualTo("done");
        }
        try (var ignored = ExecutionGate.acquire(DATABASE.getSqlSessionFactory(), scope)) {
            assertThat(ignored).isNotNull();
        }
    }

    @Test
    void tenantsExecuteConcurrentlyWithIndependentToolkitsPermissionsAndRuntimeContexts()
            throws Exception {
        var a = scope();
        var b =
                new ExecutionScope(
                        "tenant-b", a.subject(), a.conversation(), a.agent(), a.release());
        var barrier = new CountDownLatch(2);
        AtomicReference<String> seenA = new AtomicReference<>();
        AtomicReference<String> seenB = new AtomicReference<>();
        try (var executor = Executors.newFixedThreadPool(2)) {
            var fa = executor.submit(() -> runIsolated(a, "tool_a", "ALPHA", barrier, seenA));
            var fb = executor.submit(() -> runIsolated(b, "tool_b", "BETA", barrier, seenB));
            assertThat(fa.get(25, TimeUnit.SECONDS).getTextContent()).isEqualTo("ALPHA");
            assertThat(fb.get(25, TimeUnit.SECONDS).getTextContent()).isEqualTo("BETA");
        }
        assertThat(seenA.get()).isEqualTo(a.userId());
        assertThat(seenB.get()).isEqualTo(b.userId());
        assertThat(
                        new MyBatisAgentStateStore(MAPPER, a)
                                .get(a.userId(), a.sessionId(), "agent_state", AgentState.class)
                                .orElseThrow()
                                .toJson())
                .contains("ALPHA")
                .doesNotContain("BETA", "tool_b");
        assertThat(
                        new MyBatisAgentStateStore(MAPPER, b)
                                .get(b.userId(), b.sessionId(), "agent_state", AgentState.class)
                                .orElseThrow()
                                .toJson())
                .contains("BETA")
                .doesNotContain("ALPHA", "tool_a");
    }

    private Msg runIsolated(
            ExecutionScope scope,
            String name,
            String answer,
            CountDownLatch barrier,
            AtomicReference<String> seen)
            throws Exception {
        Toolkit toolkit = new Toolkit();
        toolkit.registerAgentTool(
                new ToolBase(
                        ToolBase.builder()
                                .name(name)
                                .description(name)
                                .inputSchema(Map.of("type", "object", "properties", Map.of()))) {
                    public Mono<ToolResultBlock> callAsync(ToolCallParam param) {
                        seen.set(param.getRuntimeContext().getUserId());
                        return Mono.just(ToolResultBlock.text(answer));
                    }
                });
        AtomicInteger calls = new AtomicInteger();
        Model model =
                new Model() {
                    public String getModelName() {
                        return "isolation-probe";
                    }

                    public Flux<ChatResponse> stream(
                            List<Msg> m, List<ToolSchema> tools, GenerateOptions o) {
                        assertThat(tools).extracting(ToolSchema::getName).containsExactly(name);
                        if (calls.incrementAndGet() > 1)
                            return Flux.just(
                                    ProbeFixtures.response(
                                            TextBlock.builder().text(answer).build()));
                        return simultaneousModel(barrier, answer).stream(m, tools, o)
                                .map(
                                        ignored ->
                                                ProbeFixtures.response(
                                                        ToolUseBlock.builder()
                                                                .id("same-call-id")
                                                                .name(name)
                                                                .input(Map.of())
                                                                .build()));
                    }
                };
        return new GuardedAgentRunner(DATABASE, scope)
                .run(
                        model,
                        toolkit,
                        ProbeFixtures.permissions(name, false),
                        () -> List.of(new UserMessage("user", answer)));
    }

    @Test
    void effectIdempotencyRejectsChangedPayloadAndReusesVerifiedResult() {
        var scope = scope();
        var ledger = new EffectLedger(EFFECTS, scope);
        String hash = "a".repeat(64);
        assertThat(ledger.begin("op", hash)).isEmpty();
        assertThatThrownBy(() -> ledger.begin("op", hash))
                .isInstanceOf(EffectLedger.UncertainEffectException.class);
        assertThatThrownBy(() -> ledger.begin("op", "b".repeat(64)))
                .isInstanceOf(IllegalArgumentException.class);
        ledger.complete("op", hash, "{\"ok\":true}");
        assertThat(ledger.begin("op", hash)).isPresent();
        ledger.assertNoUncertainEffects();
    }

    @Test
    void cooperativeInterruptPersistsSafeStateAndAnotherScopeContinues() throws Exception {
        var interrupted = scope();
        var healthy = scope();
        var entered = new CountDownLatch(1);
        Model streaming =
                new Model() {
                    public String getModelName() {
                        return "interrupt-probe";
                    }

                    public Flux<ChatResponse> stream(
                            List<Msg> m, List<ToolSchema> t, GenerateOptions o) {
                        return Flux.interval(Duration.ofMillis(30))
                                .map(
                                        n -> {
                                            entered.countDown();
                                            return ProbeFixtures.response(
                                                    TextBlock.builder().text("partial").build());
                                        });
                    }
                };
        try (var engine =
                        IsolatedAgentFactory.create(
                                streaming,
                                new Toolkit(),
                                new MyBatisAgentStateStore(MAPPER, interrupted),
                                ProbeFixtures.permissions("none", false));
                var executor = Executors.newSingleThreadExecutor()) {
            var outcome =
                    executor.submit(
                            () ->
                                    engine.call(
                                                    List.of(
                                                            new UserMessage(
                                                                    "user", "interrupt-me")),
                                                    IsolatedAgentFactory.context(interrupted))
                                            .block(Duration.ofSeconds(10)));
            assertThat(entered.await(5, TimeUnit.SECONDS)).isTrue();
            engine.interrupt(IsolatedAgentFactory.context(interrupted));
            assertThat(outcome.get(10, TimeUnit.SECONDS)).isNotNull();
        }
        var saved =
                new MyBatisAgentStateStore(MAPPER, interrupted)
                        .get(
                                interrupted.userId(),
                                interrupted.sessionId(),
                                "agent_state",
                                AgentState.class)
                        .orElseThrow();
        assertThat(saved.toJson()).contains("interrupt-me");
        var success =
                new GuardedAgentRunner(DATABASE, healthy)
                        .run(
                                simultaneousModel(new CountDownLatch(0), "healthy"),
                                new Toolkit(),
                                ProbeFixtures.permissions("none", false),
                                () -> List.of(new UserMessage("user", "healthy")));
        assertThat(success.getTextContent()).isEqualTo("healthy");
    }
}
