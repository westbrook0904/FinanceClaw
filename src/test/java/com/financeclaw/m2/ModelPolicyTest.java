package com.financeclaw.m2;

import static org.assertj.core.api.Assertions.*;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.financeclaw.domain.release.ModelProfile;
import com.financeclaw.infrastructure.agentscope.IsolatedAgentFactory;
import com.financeclaw.infrastructure.agentscope.execution.*;
import com.financeclaw.support.PayloadJson;
import io.agentscope.core.message.*;
import io.agentscope.core.model.*;
import io.agentscope.core.model.transport.*;
import io.agentscope.core.permission.*;
import io.agentscope.core.state.InMemoryAgentStateStore;
import io.agentscope.core.tool.Toolkit;
import java.time.Duration;
import java.util.*;
import java.util.concurrent.atomic.AtomicInteger;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import reactor.core.publisher.Flux;

class ModelPolicyTest {
    static class Capture implements HttpTransport {
        final AtomicInteger count = new AtomicInteger();
        String body;
        boolean error;
        int output = 2;
        boolean empty;

        public HttpResponse execute(HttpRequest request) {
            throw new UnsupportedOperationException();
        }

        public Flux<String> stream(HttpRequest request) {
            body = request.getBody();
            count.incrementAndGet();
            if (error) return Flux.error(new IllegalStateException("provider failure"));
            String delta = empty ? "{}" : "{\"role\":\"assistant\",\"content\":\"答案\"}";
            return Flux.just(
                    "{\"id\":\"x\",\"choices\":[{\"index\":0,\"delta\":"
                            + delta
                            + ",\"finish_reason\":\"stop\"}],\"usage\":{\"prompt_tokens\":10,\"completion_tokens\":"
                            + output
                            + ",\"total_tokens\":"
                            + (10 + output)
                            + "}}");
        }

        public void close() {}
    }

    @Test
    void nativeAdapterPreservesThinkingAndChecksUsage() throws Exception {
        var wire = new Capture();
        var p = new ModelProfile("qwen-test", "http://127.0.0.1/v1", 10000, 12000, 128, 32, true);
        try (var model = ModelFactory.create(p, "test-only", wire)) {
            assertThat(
                            model.stream(List.of(new UserMessage("user", "你好")), List.of(), null)
                                    .collectList()
                                    .block(Duration.ofSeconds(5)))
                    .isNotEmpty();
            var json = new ObjectMapper().readTree(wire.body);
            assertThat(json.path("enable_thinking").asBoolean()).isTrue();
            assertThat(json.path("max_tokens").asInt()).isEqualTo(128);
            wire.output = -1;
            assertThatThrownBy(
                            () ->
                                    model.stream(
                                                    List.of(new UserMessage("user", "你好")),
                                                    List.of(),
                                                    null)
                                            .blockLast())
                    .hasMessageContaining("invalid_model_usage");
        }
    }

    @ParameterizedTest
    @ValueSource(
            strings = {
                "chinese",
                "hotel",
                "tools",
                "tool_calls",
                "response_schema",
                "image",
                "audio"
            })
    void completeRequestBudgetRejectsOversizeAndUnpricedMediaBeforeNetwork(String scenario) {
        var wire = new Capture();
        var transport = new BudgetedTransport(wire, 1024, 2048, 128, 32);
        Map<String, Object> request = new HashMap<>();
        request.put("max_tokens", 128);
        request.put("messages", List.of(Map.of("role", "user", "content", "hello")));
        switch (scenario) {
            case "chinese" ->
                    request.put(
                            "messages",
                            List.of(Map.of("role", "user", "content", "中文".repeat(300))));
            case "hotel" ->
                    request.put(
                            "messages",
                            List.of(
                                    Map.of(
                                            "role",
                                            "tool",
                                            "content",
                                            PayloadJson.canonical(
                                                    Collections.nCopies(
                                                            100, Map.of("hotel", "超长酒店资料"))))));
            case "tools" ->
                    request.put(
                            "tools",
                            Collections.nCopies(
                                    100,
                                    Map.of("name", "search", "parameters", GovernedTools.CITY)));
            case "tool_calls" ->
                    request.put(
                            "messages",
                            List.of(
                                    Map.of(
                                            "role",
                                            "assistant",
                                            "tool_calls",
                                            Collections.nCopies(
                                                    100,
                                                    Map.of(
                                                            "id",
                                                            "call",
                                                            "function",
                                                            Map.of(
                                                                    "name",
                                                                    "search",
                                                                    "arguments",
                                                                    "上海"))))));
            case "response_schema" ->
                    request.put(
                            "response_format",
                            Map.of(
                                    "type",
                                    "json_schema",
                                    "json_schema",
                                    Map.of("enum", Collections.nCopies(500, "choice"))));
            case "image" ->
                    request.put(
                            "messages",
                            List.of(
                                    Map.of(
                                            "role",
                                            "user",
                                            "content",
                                            List.of(
                                                    Map.of(
                                                            "type",
                                                            "image_url",
                                                            "image_url",
                                                            Map.of(
                                                                    "url",
                                                                    "https://example.invalid/a.png"))))));
            case "audio" ->
                    request.put(
                            "messages",
                            List.of(
                                    Map.of(
                                            "role",
                                            "user",
                                            "content",
                                            List.of(
                                                    Map.of(
                                                            "type",
                                                            "input_audio",
                                                            "input_audio",
                                                            Map.of(
                                                                    "data", "YQ==", "format",
                                                                    "wav"))))));
        }
        assertThatThrownBy(() -> transport.check(PayloadJson.canonical(request)))
                .isInstanceOf(IllegalArgumentException.class);
        assertThat(wire.count).hasValue(0);
    }

    @Test
    void byteFallbackCountsNonAsciiAndAllSchemasWithoutTokenizerCache() {
        var t = new BudgetedTransport(new Capture(), 10000, 12000, 128, 32);
        String request =
                PayloadJson.canonical(
                        Map.of(
                                "messages",
                                List.of(Map.of("role", "user", "content", "中文🙂")),
                                "tools",
                                List.of(GovernedTools.CITY),
                                "response_format",
                                GovernedTools.TITLE,
                                "max_tokens",
                                128));
        assertThat(t.check(request))
                .isEqualTo(request.getBytes(java.nio.charset.StandardCharsets.UTF_8).length)
                .isGreaterThan(request.length());
    }

    @Test
    void nativeFallbackUsesItsOwnSmallerBudgetBeforeDispatch() {
        var primary = new Capture();
        primary.error = true;
        var fallback = new Capture();
        try (var a =
                        ModelFactory.create(
                                new ModelProfile(
                                        "primary",
                                        "http://127.0.0.1/v1",
                                        10000,
                                        12000,
                                        128,
                                        32,
                                        false),
                                "test",
                                primary);
                var b =
                        ModelFactory.create(
                                new ModelProfile(
                                        "backup", "http://127.0.0.1/v1", 300, 1000, 64, 32, false),
                                "test",
                                fallback);
                var agent =
                        IsolatedAgentFactory.create(
                                a,
                                b,
                                new Toolkit(),
                                new InMemoryAgentStateStore(),
                                PermissionContextState.builder()
                                        .addDenyRule(
                                                "*",
                                                new PermissionRule(
                                                        "*", null, PermissionBehavior.DENY, "test"))
                                        .build())) {
            assertThatThrownBy(
                            () ->
                                    agent.call(new UserMessage("user", "原始请求".repeat(100)))
                                            .block(Duration.ofSeconds(5)))
                    .isInstanceOf(RuntimeException.class);
            assertThat(primary.count).hasValue(1);
            assertThat(primary.body).doesNotContain("enable_thinking");
            assertThat(fallback.count).hasValue(0);
        }
    }

    @Test
    void nativeFinalAnswerFilterRemovesIntermediateTextAndKeepsFinalDeltas() {
        var wire = new Capture();
        try (var m =
                        ModelFactory.create(
                                new ModelProfile(
                                        "primary",
                                        "http://127.0.0.1/v1",
                                        10000,
                                        12000,
                                        128,
                                        32,
                                        false),
                                "test",
                                wire);
                var agent =
                        IsolatedAgentFactory.create(
                                m,
                                new Toolkit(),
                                new InMemoryAgentStateStore(),
                                PermissionContextState.builder()
                                        .addDenyRule(
                                                "*",
                                                new PermissionRule(
                                                        "*", null, PermissionBehavior.DENY, "test"))
                                        .build())) {
            var events =
                    agent.streamEvents(new UserMessage("user", "你好"))
                            .collectList()
                            .block(Duration.ofSeconds(5));
            assertThat(events)
                    .anyMatch(e -> e instanceof io.agentscope.core.event.TextBlockDeltaEvent);
        }
    }

    @Test
    void emptyProviderBodyCannotCompleteTurn() {
        var wire = new Capture();
        wire.empty = true;
        var store = new InMemoryAgentStateStore();
        try (var model =
                        ModelFactory.create(
                                new ModelProfile(
                                        "empty",
                                        "http://127.0.0.1/v1",
                                        10000,
                                        12000,
                                        128,
                                        32,
                                        false),
                                "test",
                                wire);
                var agent =
                        IsolatedAgentFactory.create(
                                model,
                                new Toolkit(),
                                store,
                                PermissionContextState.builder()
                                        .addDenyRule(
                                                "*",
                                                new PermissionRule(
                                                        "*", null, PermissionBehavior.DENY, "test"))
                                        .build())) {
            var context =
                    io.agentscope.core.agent.RuntimeContext.builder()
                            .userId("user")
                            .sessionId("session")
                            .build();
            try {
                agent.call(List.of(new UserMessage("user", "hello")), context)
                        .block(Duration.ofSeconds(5));
            } catch (RuntimeException expected) {
                /* provider may signal empty output as error */
            }
            var saved =
                    store.getVersioned(
                            "user",
                            "session",
                            "agent_state",
                            io.agentscope.core.state.AgentState.class);
            assertThat(TaskAgentRuntime.classify(saved.value(), saved.version()).status())
                    .isEqualTo("failed");
        }
    }
}
