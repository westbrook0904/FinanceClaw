package com.financeclaw.m0;

import static org.assertj.core.api.Assertions.*;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.financeclaw.infrastructure.agentscope.model.BudgetedTransport;
import com.sun.net.httpserver.HttpServer;
import io.agentscope.core.formatter.JsonSchema;
import io.agentscope.core.formatter.ResponseFormat;
import io.agentscope.core.message.*;
import io.agentscope.core.model.GenerateOptions;
import io.agentscope.core.model.ToolSchema;
import io.agentscope.core.model.transport.JdkHttpTransport;
import io.agentscope.extensions.model.openai.OpenAIChatModel;
import java.net.InetSocketAddress;
import java.net.http.HttpClient;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;
import org.junit.jupiter.api.Test;

class ModelPayloadTest {
    @Test
    void realOpenAiAdapterSendsBoundedMessagesToolsAndResponseSchemaWithoutLocalMetadata()
            throws Exception {
        HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        AtomicReference<String> payload = new AtomicReference<>();
        AtomicInteger requests = new AtomicInteger();
        server.createContext(
                "/v1/chat/completions",
                exchange -> {
                    requests.incrementAndGet();
                    payload.set(
                            new String(
                                    exchange.getRequestBody().readAllBytes(),
                                    StandardCharsets.UTF_8));
                    byte[] answer =
                            new ObjectMapper()
                                    .writeValueAsBytes(
                                            Map.of(
                                                    "id",
                                                    "m0",
                                                    "object",
                                                    "chat.completion",
                                                    "created",
                                                    1,
                                                    "model",
                                                    "m0-model",
                                                    "choices",
                                                    List.of(
                                                            Map.of(
                                                                    "index",
                                                                    0,
                                                                    "finish_reason",
                                                                    "stop",
                                                                    "message",
                                                                    Map.of(
                                                                            "role",
                                                                            "assistant",
                                                                            "content",
                                                                            "{\"answer\":\"ok\"}"))),
                                                    "usage",
                                                    Map.of(
                                                            "prompt_tokens",
                                                            10,
                                                            "completion_tokens",
                                                            5,
                                                            "total_tokens",
                                                            15)));
                    exchange.getResponseHeaders().set("Content-Type", "application/json");
                    exchange.sendResponseHeaders(200, answer.length);
                    exchange.getResponseBody().write(answer);
                    exchange.close();
                });
        server.start();
        var transport =
                new BudgetedTransport(
                        JdkHttpTransport.builder()
                                .client(
                                        HttpClient.newBuilder()
                                                .version(HttpClient.Version.HTTP_1_1)
                                                .build())
                                .build(),
                        10_000,
                        12_000,
                        128,
                        32);
        try {
            var model =
                    OpenAIChatModel.builder()
                            .apiKey("m0-test-not-a-real-key")
                            .modelName("m0-model")
                            .baseUrl("http://127.0.0.1:" + server.getAddress().getPort() + "/v1")
                            .stream(false)
                            .httpTransport(transport)
                            .build();
            var schema =
                    Map.<String, Object>of(
                            "type",
                            "object",
                            "properties",
                            Map.of("city", Map.of("type", "string")),
                            "required",
                            List.of("city"),
                            "additionalProperties",
                            false);
            var tools =
                    List.of(
                            ToolSchema.builder()
                                    .name("search_hotels")
                                    .description("酒店查询")
                                    .parameters(schema)
                                    .build());
            var user =
                    new UserMessage("user", "查询上海酒店")
                            .withMetadata(
                                    Map.of(
                                            "artifact",
                                            "LOCAL_ONLY_".repeat(10_000),
                                            "audit",
                                            "NOT_PROVIDER_INPUT"));
            var messages =
                    List.<Msg>of(
                            new SystemMessage("Use bounded evidence"),
                            user,
                            AssistantMessage.builder()
                                    .name("assistant")
                                    .content(
                                            ToolUseBlock.builder()
                                                    .id("hotel-call")
                                                    .name("search_hotels")
                                                    .input(Map.of("city", "上海"))
                                                    .build())
                                    .build(),
                            new ToolResultMessage("hotel-call", "search_hotels", "{\"count\":2}"));
            var responseSchema =
                    JsonSchema.builder()
                            .name("answer")
                            .strict(true)
                            .schema(
                                    Map.of(
                                            "type",
                                            "object",
                                            "properties",
                                            Map.of("answer", Map.of("type", "string")),
                                            "required",
                                            List.of("answer"),
                                            "additionalProperties",
                                            false))
                            .build();
            var options =
                    GenerateOptions.builder()
                            .maxTokens(128)
                            .temperature(0.0)
                            .responseFormat(ResponseFormat.jsonSchema(responseSchema))
                            .build();
            var result =
                    model.stream(messages, tools, options)
                            .collectList()
                            .block(Duration.ofSeconds(10));
            assertThat(result).isNotEmpty();
            assertThat(requests).hasValue(1);
            JsonNode wire = new ObjectMapper().readTree(payload.get());
            assertThat(wire.path("messages")).hasSize(4);
            assertThat(wire.path("messages").get(2).path("tool_calls").get(0).path("id").asText())
                    .isEqualTo("hotel-call");
            assertThat(wire.path("messages").get(3).path("tool_call_id").asText())
                    .isEqualTo("hotel-call");
            assertThat(
                            wire.path("tools")
                                    .get(0)
                                    .path("function")
                                    .path("parameters")
                                    .path("required")
                                    .get(0)
                                    .asText())
                    .isEqualTo("city");
            assertThat(wire.path("response_format").path("type").asText()).isEqualTo("json_schema");
            assertThat(payload.get())
                    .doesNotContain("LOCAL_ONLY", "NOT_PROVIDER_INPUT", "artifact");
            assertThat(transport.check(payload.get()))
                    .isEqualTo(payload.get().getBytes(StandardCharsets.UTF_8).length);
            Files.createDirectories(Path.of("target/m0"));
            Files.writeString(
                    Path.of("target/m0/openai-request.json"),
                    new ObjectMapper().writerWithDefaultPrettyPrinter().writeValueAsString(wire));
            var huge = new UserMessage("user", "超长输入".repeat(5_000));
            assertThatThrownBy(
                            () ->
                                    model.stream(List.of(huge), tools, options)
                                            .collectList()
                                            .block(Duration.ofSeconds(10)))
                    .isInstanceOf(RuntimeException.class);
            assertThat(requests)
                    .hasValue(1); // Oversize input was rejected before network dispatch.
        } finally {
            transport.close();
            server.stop(0);
        }
    }

    @Test
    void fallbackBudgetIncludesSchemaAndPreservesOutputReserve() {
        var transport = new BudgetedTransport(null, 1000, 300, 100, 10);
        assertThatThrownBy(() -> transport.check("{\"messages\":[],\"max_tokens\":101}"))
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining("output");
        assertThatThrownBy(
                        () ->
                                transport.check(
                                        "{\"messages\":[],\"max_tokens\":100,\"tools\":[\""
                                                + "x".repeat(200)
                                                + "\"]}"))
                .isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining("input");
        assertThatThrownBy(() -> transport.check("{\"messages\":[]}"))
                .isInstanceOf(IllegalArgumentException.class);
    }
}
