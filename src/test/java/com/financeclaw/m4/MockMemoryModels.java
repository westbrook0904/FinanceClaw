package com.financeclaw.m4;

import com.fasterxml.jackson.databind.*;
import com.financeclaw.support.PayloadJson;
import com.sun.net.httpserver.HttpServer;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.util.*;
import java.util.concurrent.*;

final class MockMemoryModels implements AutoCloseable {
  final HttpServer server;
  final ExecutorService executor = Executors.newVirtualThreadPerTaskExecutor();
  final List<JsonNode> requests = new CopyOnWriteArrayList<>();
  volatile int dimensions = 1024;

  MockMemoryModels() {
    try {
      server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
    } catch (Exception e) {
      throw new IllegalStateException(e);
    }
    server.setExecutor(executor);
    server.createContext(
        "/",
        exchange -> {
          var body = new ObjectMapper().readTree(exchange.getRequestBody());
          requests.add(body);
          String response;
          if (exchange.getRequestURI().getPath().endsWith("embeddings")) {
            var vector = new ArrayList<Double>(Collections.nCopies(dimensions, 0.0));
            vector.set(0, 1.0);
            response =
                PayloadJson.canonical(
                    Map.of(
                        "object",
                        "list",
                        "model",
                        "embedding-test",
                        "data",
                        List.of(Map.of("object", "embedding", "index", 0, "embedding", vector)),
                        "usage",
                        Map.of("prompt_tokens", 3, "total_tokens", 3)));
            exchange.getResponseHeaders().set("Content-Type", "application/json");
          } else {
            String result =
                PayloadJson.canonical(
                    Map.of(
                        "proposals",
                        List.of(Map.of("key", "home_city", "value", "上海", "quote", "我住在上海"))));
            Object delta =
                body.has("response_format")
                    ? Map.of("content", result)
                    : Map.of(
                        "tool_calls",
                        List.of(
                            Map.of(
                                "index",
                                0,
                                "id",
                                "memory-output",
                                "type",
                                "function",
                                "function",
                                Map.of(
                                    "name",
                                    "generate_response",
                                    "arguments",
                                    "{\"response\":" + result + "}"))));
            response =
                "data: "
                    + PayloadJson.canonical(
                        Map.of(
                            "id",
                            "extract",
                            "object",
                            "chat.completion.chunk",
                            "model",
                            "memory-test",
                            "choices",
                            List.of(Map.of("index", 0, "delta", delta))))
                    + "\n\ndata: [DONE]\n\n";
            exchange.getResponseHeaders().set("Content-Type", "text/event-stream");
          }
          byte[] bytes = response.getBytes(StandardCharsets.UTF_8);
          exchange.sendResponseHeaders(200, bytes.length);
          exchange.getResponseBody().write(bytes);
          exchange.close();
        });
    server.start();
  }

  String url() {
    return "http://127.0.0.1:" + server.getAddress().getPort() + "/v1";
  }

  public void close() {
    server.stop(0);
    executor.shutdownNow();
  }
}
