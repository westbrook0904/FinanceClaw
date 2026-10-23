package com.financeclaw.m3;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.financeclaw.domain.release.RemoteEndpoint;
import com.financeclaw.infrastructure.release.BusinessResources;
import com.financeclaw.support.PayloadJson;
import com.financeclaw.tools.*;
import com.sun.net.httpserver.HttpServer;
import java.net.InetSocketAddress;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicInteger;

final class MockRemote implements AutoCloseable {
    final HttpServer server;
    final ExecutorService executor = Executors.newCachedThreadPool();
    final Map<String, String> contracts = new TreeMap<>();
    final AtomicInteger calls = new AtomicInteger();
    volatile boolean annotations, drift, error, identityDrift, structuredOnly;
    volatile long delay;
    volatile String authorization;
    volatile Object payload =
            Map.of(
                    "hotels",
                    java.util.stream.IntStream.range(0, 800)
                            .mapToObj(
                                    i ->
                                            Map.of(
                                                    "name",
                                                    "酒店" + i,
                                                    "city",
                                                    "上海",
                                                    "description",
                                                    "A".repeat(80)))
                            .toList());

    MockRemote(String kind) throws Exception {
        var config =
                PayloadJson.object(BusinessResources.read("classpath:contracts/" + kind + ".json"));
        if (kind.equals("hotel"))
            for (var t : (List<?>) config.get("tools")) {
                var tool = (Map<?, ?>) t;
                contracts.put(
                        (String) tool.get("name"), PayloadJson.canonical(tool.get("inputSchema")));
            }
        else
            for (var name : List.of("almanac", "bazi")) {
                contracts.put(
                        name,
                        PayloadJson.canonical(((Map<?, ?>) config.get(name)).get("inputSchema")));
                contracts.put(
                        name + ":output",
                        PayloadJson.canonical(((Map<?, ?>) config.get(name)).get("outputSchema")));
            }
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.setExecutor(executor);
        server.createContext(
                "/mcp",
                exchange -> {
                    try {
                        authorization = exchange.getRequestHeaders().getFirst("Authorization");
                        if (!exchange.getRequestMethod().equals("POST")) {
                            exchange.sendResponseHeaders(405, -1);
                            return;
                        }
                        var json = new ObjectMapper();
                        var request = json.readTree(exchange.getRequestBody());
                        if (!request.has("id")) {
                            exchange.sendResponseHeaders(202, -1);
                            return;
                        }
                        Object result =
                                switch (request.path("method").asText()) {
                                    case "initialize" ->
                                            Map.of(
                                                    "protocolVersion",
                                                    request.path("params")
                                                            .path("protocolVersion")
                                                            .asText(),
                                                    "capabilities",
                                                    Map.of("tools", Map.of("listChanged", false)),
                                                    "serverInfo",
                                                    Map.of(
                                                            "name",
                                                            identityDrift ? "changed" : "fixture",
                                                            "version",
                                                            "1"));
                                    case "tools/list" ->
                                            Map.of(
                                                    "tools",
                                                    contracts.entrySet().stream()
                                                            .filter(
                                                                    e ->
                                                                            !e.getKey()
                                                                                    .endsWith(
                                                                                            ":output"))
                                                            .map(
                                                                    e -> {
                                                                        var schema =
                                                                                new TreeMap<>(
                                                                                        PayloadJson
                                                                                                .object(
                                                                                                        e
                                                                                                                .getValue()));
                                                                        if (annotations) {
                                                                            schema.put(
                                                                                    "title",
                                                                                    "new text");
                                                                            schema.put(
                                                                                    "description",
                                                                                    "new wording");
                                                                            schema.put(
                                                                                    "default",
                                                                                    Map.of());
                                                                        }
                                                                        if (drift)
                                                                            schema.put(
                                                                                    "required",
                                                                                    List.of(
                                                                                            "injected_required_parameter"));
                                                                        var tool =
                                                                                new LinkedHashMap<
                                                                                        String,
                                                                                        Object>();
                                                                        tool.put(
                                                                                "name", e.getKey());
                                                                        tool.put(
                                                                                "description",
                                                                                "fixture");
                                                                        tool.put(
                                                                                "inputSchema",
                                                                                schema);
                                                                        String output =
                                                                                contracts.get(
                                                                                        e.getKey()
                                                                                                + ":output");
                                                                        if (output != null) {
                                                                            tool.put(
                                                                                    "outputSchema",
                                                                                    PayloadJson
                                                                                            .object(
                                                                                                    output));
                                                                        }
                                                                        return tool;
                                                                    })
                                                            .toList());
                                    case "tools/call" -> {
                                        calls.incrementAndGet();
                                        if (delay > 0) Thread.sleep(delay);
                                        yield Map.of(
                                                "content",
                                                List.of(
                                                        Map.of(
                                                                "type",
                                                                "text",
                                                                "text",
                                                                structuredOnly
                                                                        ? ""
                                                                        : json.writeValueAsString(
                                                                                payload))),
                                                "structuredContent",
                                                payload,
                                                "isError",
                                                error);
                                    }
                                    default -> Map.of();
                                };
                        byte[] bytes =
                                json.writeValueAsBytes(
                                        Map.of(
                                                "jsonrpc",
                                                "2.0",
                                                "id",
                                                json.convertValue(request.get("id"), Object.class),
                                                "result",
                                                result));
                        exchange.getResponseHeaders().set("Content-Type", "application/json");
                        exchange.sendResponseHeaders(200, bytes.length);
                        exchange.getResponseBody().write(bytes);
                    } catch (Exception ignored) {
                        /* client timeout intentionally closes the response */
                    } finally {
                        exchange.close();
                    }
                });
        server.start();
    }

    RemoteEndpoint definition(String service, boolean external, int timeout) {
        return new RemoteEndpoint(
                service,
                "http://127.0.0.1:" + server.getAddress().getPort() + "/mcp",
                List.of("127.0.0.1:" + server.getAddress().getPort()),
                "",
                external,
                List.of("local"),
                contracts.keySet().stream().filter(n -> !n.endsWith(":output")).sorted().toList(),
                timeout);
    }

    public void close() {
        server.stop(0);
        executor.shutdownNow();
    }
}
