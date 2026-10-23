package com.financeclaw.infrastructure.agentscope.capability;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.financeclaw.domain.release.*;
import com.sun.net.httpserver.HttpServer;
import java.net.InetSocketAddress;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicInteger;

/** Real HTTP MCP protocol; mutable discovery data never invokes a business tool. */
public final class McpContractFixture implements AutoCloseable {
    private final HttpServer server;
    private final ExecutorService executor = Executors.newCachedThreadPool();
    public final AtomicInteger lists = new AtomicInteger();
    public final AtomicInteger calls = new AtomicInteger();
    public volatile List<Map<String, Object>> tools =
            List.of(tool("search", 100, "string"), tool("unused", 100, "string"));
    public volatile long listDelayMillis;

    public McpContractFixture() throws Exception {
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.setExecutor(executor);
        server.createContext(
                "/mcp",
                exchange -> {
                    try {
                        if (!"POST".equals(exchange.getRequestMethod())) {
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
                                                            Map.of(
                                                                    "tools",
                                                                    Map.of("listChanged", false)),
                                                    "serverInfo",
                                                            Map.of(
                                                                    "name",
                                                                    "contracts",
                                                                    "version",
                                                                    "1"));
                                    case "tools/list" -> {
                                        lists.incrementAndGet();
                                        if (listDelayMillis > 0) Thread.sleep(listDelayMillis);
                                        yield Map.of("tools", tools);
                                    }
                                    case "tools/call" -> {
                                        calls.incrementAndGet();
                                        yield Map.of(
                                                "content",
                                                List.of(
                                                        Map.of(
                                                                "type",
                                                                "text",
                                                                "text",
                                                                "{\"hotels\":[]}")),
                                                "isError",
                                                false);
                                    }
                                    default -> Map.of();
                                };
                        byte[] response =
                                json.writeValueAsBytes(
                                        Map.of(
                                                "jsonrpc",
                                                "2.0",
                                                "id",
                                                json.convertValue(request.get("id"), Object.class),
                                                "result",
                                                result));
                        exchange.getResponseHeaders().set("Content-Type", "application/json");
                        exchange.sendResponseHeaders(200, response.length);
                        exchange.getResponseBody().write(response);
                    } catch (Exception ignored) {
                        // Cancellation and timeout intentionally close this fixture's response
                        // stream.
                    } finally {
                        exchange.close();
                    }
                });
        server.start();
    }

    public static Map<String, Object> tool(String name, int maxLength, String outputType) {
        return Map.of(
                "name",
                name,
                "description",
                "Hotel lookup",
                "inputSchema",
                Map.of(
                        "type",
                        "object",
                        "properties",
                        Map.of(
                                "city", Map.of("type", "string", "maxLength", maxLength),
                                "description", Map.of("type", "string")),
                        "required",
                        List.of("city")),
                "outputSchema",
                Map.of("type", "object", "properties", Map.of("value", Map.of("type", outputType))),
                "annotations",
                Map.of("readOnlyHint", true));
    }

    public RemoteEndpoint remote(String service, String credential) {
        int port = server.getAddress().getPort();
        return new RemoteEndpoint(
                service,
                "http://127.0.0.1:" + port + "/mcp",
                List.of("127.0.0.1:" + port),
                credential,
                false,
                List.of("tenant"),
                List.of("search", "unused"),
                1500);
    }

    public static ReleaseDefinition release(RemoteEndpoint... remotes) {
        return new ReleaseDefinition(
                "test",
                "finance_agent",
                List.of("travel:read", "taibu:read", "artifact:read"),
                ReleaseDefinition.OFFLINE,
                null,
                10,
                10,
                new BusinessFeatures(List.of(remotes), "", "{}", null));
    }

    @Override
    public void close() {
        server.stop(0);
        executor.shutdownNow();
    }
}
