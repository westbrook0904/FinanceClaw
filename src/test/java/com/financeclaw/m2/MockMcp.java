package com.financeclaw.m2;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.financeclaw.agent.GovernedTools;
import com.sun.net.httpserver.HttpServer;
import java.net.InetSocketAddress;
import java.util.*;
import java.util.concurrent.atomic.AtomicInteger;

/** Loopback JSON-RPC fixture; the production client is the SDK's real Streamable HTTP transport. */
final class MockMcp implements AutoCloseable {
  final HttpServer server;
  final AtomicInteger calls = new AtomicInteger();
  volatile boolean drift;

  MockMcp() throws Exception {
    server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
    server.createContext(
        "/mcp",
        exchange -> {
          if (!exchange.getRequestMethod().equals("POST")) {
            exchange.sendResponseHeaders(405, -1);
            exchange.close();
            return;
          }
          var json = new ObjectMapper();
          var request = json.readTree(exchange.getRequestBody());
          if (!request.has("id")) {
            exchange.sendResponseHeaders(202, -1);
            exchange.close();
            return;
          }
          Object result =
              switch (request.path("method").asText()) {
                case "initialize" ->
                    Map.of(
                        "protocolVersion",
                        request.path("params").path("protocolVersion").asText(),
                        "capabilities",
                        Map.of("tools", Map.of("listChanged", false)),
                        "serverInfo",
                        Map.of("name", "m2-mock", "version", "1"));
                case "tools/list" ->
                    Map.of(
                        "tools",
                        List.of(
                            Map.of(
                                "name",
                                "hotels",
                                "description",
                                "fixture",
                                "inputSchema",
                                drift ? GovernedTools.TITLE : GovernedTools.CITY)));
                case "tools/call" -> {
                  calls.incrementAndGet();
                  yield Map.of(
                      "content",
                      List.of(Map.of("type", "text", "text", "PRIVATE_MCP_RAW_RESULT")),
                      "structuredContent",
                      Map.of("hotels", List.of(Map.of("city", "上海", "name", "fixture"))),
                      "isError",
                      false);
                }
                default -> Map.of();
              };
          byte[] body =
              json.writeValueAsBytes(
                  Map.of(
                      "jsonrpc",
                      "2.0",
                      "id",
                      json.convertValue(request.get("id"), Object.class),
                      "result",
                      result));
          exchange.getResponseHeaders().set("Content-Type", "application/json");
          exchange.sendResponseHeaders(200, body.length);
          exchange.getResponseBody().write(body);
          exchange.close();
        });
    server.start();
  }

  String url() {
    return "http://127.0.0.1:" + server.getAddress().getPort() + "/mcp";
  }

  public void close() {
    server.stop(0);
  }
}
