package com.financeclaw.p3;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import java.io.IOException;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.function.Function;

/** 本机 OpenAI SSE 协议替身；应用仍使用真实 ModelFactory、HTTP transport 和原生 SDK。 */
final class PrivateAgentLoopback implements AutoCloseable {
    static final String PARENT = "parent-loopback";
    static final String LEAF = "leaf-loopback";
    private final ObjectMapper json = new ObjectMapper();
    private final HttpServer server;
    private final ExecutorService executor = Executors.newVirtualThreadPerTaskExecutor();
    private final List<Request> requests = new CopyOnWriteArrayList<>();
    private final List<Throwable> failures = new CopyOnWriteArrayList<>();
    private final List<Gate> gates = new CopyOnWriteArrayList<>();
    private final Map<String, AtomicInteger> rounds = new ConcurrentHashMap<>();
    private final AtomicInteger activeHandlers = new AtomicInteger();
    private volatile Function<Request, Reply> script = request -> Reply.text("unconfigured");

    record Request(String model, int round, JsonNode payload) {
        String serialized() {
            return payload.toString();
        }

        List<String> toolNames() {
            var names = new ArrayList<String>();
            for (var tool : payload.path("tools"))
                names.add(tool.path("function").path("name").asText());
            return List.copyOf(names);
        }

        /** 直接检查真实 HTTP wire 的 schema，避免仅凭模型发出的工具名推断子工具已注册。 */
        JsonNode toolSchema(String name) {
            for (var tool : payload.path("tools")) {
                var function = tool.path("function");
                if (function.path("name").asText().equals(name)) {
                    return function.path("parameters");
                }
            }
            throw new AssertionError("Tool is absent from model request: " + name);
        }

        String toolResult(String callId) {
            var results = toolResults(callId);
            return results.isEmpty() ? null : results.getFirst();
        }

        /** 读取当前 wire 的匹配结果；条数取决于 SDK 已完成 callId 语义，不能假设必然追加第二份。 */
        List<String> toolResults(String callId) {
            var results = new ArrayList<String>();
            for (var message : payload.path("messages")) {
                if (message.path("role").asText().equals("tool")
                        && message.path("tool_call_id").asText().equals(callId)) {
                    var content = message.path("content");
                    results.add(content.isTextual() ? content.asText() : content.toString());
                }
            }
            return List.copyOf(results);
        }

        String lastToolResult(String callId) {
            var results = toolResults(callId);
            return results.isEmpty() ? null : results.getLast();
        }
    }

    record Reply(
            String text,
            String callId,
            String tool,
            Map<String, Object> arguments,
            Gate gate,
            boolean fragmented,
            List<Reply> batch) {
        Reply(
                String text,
                String callId,
                String tool,
                Map<String, Object> arguments,
                Gate gate,
                boolean fragmented) {
            this(text, callId, tool, arguments, gate, fragmented, List.of());
        }

        static Reply tools(Reply... calls) {
            if (calls.length < 1) throw new IllegalArgumentException("Empty batch");
            return new Reply(
                    null,
                    calls[0].callId(),
                    calls[0].tool(),
                    calls[0].arguments(),
                    null,
                    false,
                    List.of(calls));
        }

        static Reply text(String text) {
            return new Reply(text, null, null, null, null, false);
        }

        static Reply tool(String callId, String tool, Map<String, Object> arguments) {
            return new Reply(null, callId, tool, arguments, null, false);
        }

        /** 后续参数片段故意省略 id/name，复现 OpenAI SSE 参数续片而非两次完整工具调用。 */
        static Reply fragmentedTool(String callId, String tool, Map<String, Object> arguments) {
            return new Reply(null, callId, tool, arguments, null, true);
        }

        Reply held(Gate gate) {
            return new Reply(text, callId, tool, arguments, gate, fragmented, batch);
        }
    }

    static final class Gate {
        private final CountDownLatch entered = new CountDownLatch(1);
        private final CountDownLatch release = new CountDownLatch(1);
        private volatile boolean permitsDisconnect;
        private volatile boolean responseAttempted;

        boolean awaitEntered(Duration timeout) throws InterruptedException {
            return entered.await(timeout.toMillis(), TimeUnit.MILLISECONDS);
        }

        void releaseAfterInvalidation() {
            permitsDisconnect = true;
            release.countDown();
        }

        void release() {
            release.countDown();
        }

        boolean responseAttempted() {
            return responseAttempted;
        }

        private void awaitRelease() throws InterruptedException {
            entered.countDown();
            if (!release.await(8, TimeUnit.SECONDS))
                throw new IllegalStateException("Synthetic SSE gate timed out");
        }
    }

    PrivateAgentLoopback() throws IOException {
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.setExecutor(executor);
        server.createContext("/v1/chat/completions", this::handle);
        server.start();
    }

    String baseUrl() {
        return "http://127.0.0.1:" + server.getAddress().getPort() + "/v1";
    }

    void reset(Function<Request, Reply> script) {
        releaseAll();
        awaitIdle(Duration.ofSeconds(5));
        requests.clear();
        rounds.clear();
        failures.clear();
        gates.clear();
        this.script = script;
    }

    Gate gate() {
        var gate = new Gate();
        gates.add(gate);
        return gate;
    }

    List<Request> requests(String model) {
        return requests.stream().filter(r -> r.model().equals(model)).toList();
    }

    List<Request> requests() {
        return List.copyOf(requests);
    }

    List<Throwable> failures() {
        return List.copyOf(failures);
    }

    void releaseAll() {
        gates.forEach(Gate::release);
    }

    /** 等待已入站请求收尾，不能把根业务完成误当作本机 HTTP handler 已退出。 */
    void awaitIdle(Duration timeout) {
        long deadline = System.nanoTime() + timeout.toNanos();
        while (activeHandlers.get() != 0) {
            if (System.nanoTime() >= deadline)
                throw new AssertionError("Synthetic SSE handlers did not finish");
            try {
                Thread.sleep(10);
            } catch (InterruptedException failure) {
                Thread.currentThread().interrupt();
                throw new IllegalStateException(failure);
            }
        }
    }

    private void handle(HttpExchange exchange) {
        activeHandlers.incrementAndGet();
        Reply reply = null;
        try {
            if (!exchange.getRequestMethod().equals("POST"))
                throw new IllegalStateException("Model endpoint only accepts POST");
            var payload = json.readTree(exchange.getRequestBody());
            String model = payload.path("model").asText();
            if (!List.of(PARENT, LEAF).contains(model))
                throw new IllegalStateException("Unexpected synthetic model identity");
            if (!payload.path("stream").asBoolean())
                throw new IllegalStateException("Production model must use SSE streaming");
            var request =
                    new Request(
                            model,
                            rounds.computeIfAbsent(model, ignored -> new AtomicInteger())
                                    .incrementAndGet(),
                            payload);
            requests.add(request);
            reply = script.apply(request);
            if (reply == null)
                throw new IllegalStateException("Synthetic model script returned no reply");
            if (reply.gate() != null) reply.gate().awaitRelease();
            Map<String, Object> delta =
                    reply.tool() == null
                            ? Map.of("role", "assistant", "content", reply.text())
                            : Map.of(
                                    "role",
                                    "assistant",
                                    "tool_calls",
                                    List.of(
                                            Map.of(
                                                    "index",
                                                    0,
                                                    "id",
                                                    reply.callId(),
                                                    "type",
                                                    "function",
                                                    "function",
                                                    Map.of(
                                                            "name",
                                                            reply.tool(),
                                                            "arguments",
                                                            json.writeValueAsString(
                                                                    reply.arguments())))));
            if (!reply.batch().isEmpty()) {
                var batch = new ArrayList<Map<String, Object>>();
                for (int index = 0; index < reply.batch().size(); index++) {
                    var call = reply.batch().get(index);
                    batch.add(
                            Map.of(
                                    "index",
                                    index,
                                    "id",
                                    call.callId(),
                                    "type",
                                    "function",
                                    "function",
                                    Map.of(
                                            "name",
                                            call.tool(),
                                            "arguments",
                                            json.writeValueAsString(call.arguments()))));
                }
                delta = Map.of("role", "assistant", "tool_calls", batch);
            }
            StringBuilder stream = new StringBuilder();
            if (reply.fragmented()) {
                String arguments = json.writeValueAsString(reply.arguments());
                int middle = arguments.length() / 2;
                var header =
                        Map.of(
                                "role",
                                "assistant",
                                "tool_calls",
                                List.of(
                                        Map.of(
                                                "index",
                                                0,
                                                "id",
                                                reply.callId(),
                                                "type",
                                                "function",
                                                "function",
                                                Map.of(
                                                        "name",
                                                        reply.tool(),
                                                        "arguments",
                                                        arguments.substring(0, middle)))));
                stream.append("data: ")
                        .append(
                                json.writeValueAsString(
                                        Map.of(
                                                "id",
                                                "loopback-" + model + "-" + request.round(),
                                                "object",
                                                "chat.completion.chunk",
                                                "created",
                                                1,
                                                "model",
                                                model,
                                                "choices",
                                                List.of(Map.of("index", 0, "delta", header)))))
                        .append("\n\n");
                delta =
                        Map.of(
                                "tool_calls",
                                List.of(
                                        Map.of(
                                                "index",
                                                0,
                                                "function",
                                                Map.of("arguments", arguments.substring(middle)))));
            }
            var frame =
                    Map.of(
                            "id",
                            "loopback-" + model + "-" + request.round(),
                            "object",
                            "chat.completion.chunk",
                            "created",
                            1,
                            "model",
                            model,
                            "choices",
                            List.of(
                                    Map.of(
                                            "index",
                                            0,
                                            "delta",
                                            delta,
                                            "finish_reason",
                                            reply.tool() == null ? "stop" : "tool_calls")),
                            "usage",
                            Map.of(
                                    "prompt_tokens",
                                    40,
                                    "completion_tokens",
                                    16,
                                    "total_tokens",
                                    56));
            byte[] bytes =
                    stream.append("data: ")
                            .append(json.writeValueAsString(frame))
                            .append("\n\ndata: [DONE]\n\n")
                            .toString()
                            .getBytes(StandardCharsets.UTF_8);
            exchange.getResponseHeaders().set("Content-Type", "text/event-stream; charset=utf-8");
            exchange.getResponseHeaders().set("Cache-Control", "no-cache");
            if (reply.gate() != null) reply.gate().responseAttempted = true;
            exchange.sendResponseHeaders(200, 0);
            exchange.getResponseBody().write(bytes);
            exchange.getResponseBody().flush();
        } catch (Throwable failure) {
            // Cancelling/revoking an in-flight child can close its actual JDK HTTP connection.
            // Only that explicitly invalidated gate permits a disconnect; script errors still fail.
            boolean expectedDisconnect =
                    failure instanceof IOException
                            && reply != null
                            && reply.gate() != null
                            && reply.gate().permitsDisconnect;
            if (!expectedDisconnect) failures.add(failure);
            try {
                exchange.sendResponseHeaders(500, -1);
            } catch (IOException ignored) {
            }
            if (failure instanceof InterruptedException) Thread.currentThread().interrupt();
        } finally {
            try {
                exchange.close();
            } finally {
                activeHandlers.decrementAndGet();
            }
        }
    }

    @Override
    public void close() {
        releaseAll();
        server.stop(0);
        executor.shutdownNow();
        try {
            if (!executor.awaitTermination(5, TimeUnit.SECONDS))
                throw new IllegalStateException("Loopback model handlers did not stop");
        } catch (InterruptedException failure) {
            Thread.currentThread().interrupt();
            throw new IllegalStateException(failure);
        }
    }
}
