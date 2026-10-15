package com.financeclaw.m4;

import com.fasterxml.jackson.databind.*;
import com.financeclaw.agent.ResumeInputs;
import com.sun.net.httpserver.HttpServer;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.*;

final class MockFeishu implements AutoCloseable {
  final HttpServer server;
  final ExecutorService executor = Executors.newVirtualThreadPerTaskExecutor();
  final ObjectMapper json = new ObjectMapper();
  final AtomicInteger sends = new AtomicInteger(), tokens = new AtomicInteger();
  final List<JsonNode> writes = new CopyOnWriteArrayList<>();
  volatile int code, httpStatus;

  MockFeishu() {
    try {
      server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
    } catch (Exception e) {
      throw new IllegalStateException(e);
    }
    server.setExecutor(executor);
    server.createContext(
        "/",
        exchange -> {
          String path = exchange.getRequestURI().getPath();
          var body = json.readTree(exchange.getRequestBody());
          Object result;
          int status = 200;
          if (path.endsWith("tenant_access_token/internal")) {
            tokens.incrementAndGet();
            result = Map.of("code", 0, "tenant_access_token", "mock-token", "expire", 7200);
          } else if (path.equals("/open-apis/im/v1/messages")) {
            sends.incrementAndGet();
            writes.add(body);
            status = httpStatus == 0 ? 200 : httpStatus;
            result = Map.of("code", code, "data", Map.of("message_id", "om_reply_" + sends.get()));
          } else {
            status = 404;
            result = Map.of("code", 999);
          }
          byte[] bytes = ResumeInputs.canonical(result).getBytes(StandardCharsets.UTF_8);
          exchange.getResponseHeaders().set("Content-Type", "application/json");
          exchange.sendResponseHeaders(status, bytes.length);
          exchange.getResponseBody().write(bytes);
          exchange.close();
        });
    server.start();
  }

  String url() {
    return "http://127.0.0.1:" + server.getAddress().getPort();
  }

  void reset() {
    sends.set(0);
    writes.clear();
    code = 0;
    httpStatus = 0;
  }

  public void close() {
    server.stop(0);
    executor.shutdownNow();
  }
}
