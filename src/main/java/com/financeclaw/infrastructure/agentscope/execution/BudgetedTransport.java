package com.financeclaw.infrastructure.agentscope.execution;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import io.agentscope.core.model.transport.HttpRequest;
import io.agentscope.core.model.transport.HttpResponse;
import io.agentscope.core.model.transport.HttpTransport;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import reactor.core.publisher.Flux;

/** Check the actual adapter payload, including tool/output schemas; unpriced media fails closed. */
public final class BudgetedTransport implements HttpTransport {
  private final HttpTransport delegate;
  private final int capacity;
  private final int outputReserve;

  public BudgetedTransport(
      HttpTransport delegate,
      int inputLimit,
      int contextWindow,
      int outputReserve,
      int safetyMargin) {
    if (inputLimit < 1 || contextWindow < 1 || outputReserve < 1 || safetyMargin < 0)
      throw new IllegalArgumentException("Invalid model budget");
    this.delegate = delegate;
    this.outputReserve = outputReserve;
    this.capacity = Math.min(inputLimit, contextWindow - outputReserve) - safetyMargin;
    if (capacity < 1) throw new IllegalArgumentException("No input capacity remains");
  }

  public int check(String body) {
    if (body == null) throw new IllegalArgumentException("Missing model request body");
    try {
      JsonNode request = new ObjectMapper().readTree(body);
      if (!request.isObject() || !request.path("messages").isArray())
        throw new IllegalArgumentException("Unsupported model request shape");
      rejectUnpricedMedia(request.path("messages"));
      int output =
          request.path("max_completion_tokens").asInt(request.path("max_tokens").asInt(-1));
      if (output < 1 || output > outputReserve)
        throw new IllegalArgumentException(
            "Model output exceeds reserved capacity or is unspecified");
    } catch (IOException error) {
      throw new IllegalArgumentException("Invalid model JSON", error);
    }
    int estimatedTokens = body.getBytes(StandardCharsets.UTF_8).length;
    if (estimatedTokens > capacity)
      throw new IllegalArgumentException(
          "Model input exceeds UTF-8 fallback budget: " + estimatedTokens + " > " + capacity);
    return estimatedTokens;
  }

  private static void rejectUnpricedMedia(JsonNode node) {
    if (node.isObject()) {
      String type = node.path("type").asText();
      if (type.equals("image_url")
          || type.equals("input_audio")
          || type.equals("video_url")
          || type.equals("file"))
        throw new IllegalArgumentException("Multimodal input requires an explicit modality budget");
    }
    if (node.isContainerNode()) node.forEach(BudgetedTransport::rejectUnpricedMedia);
  }

  @Override
  public HttpResponse execute(HttpRequest request) {
    check(request.getBody());
    return delegate.execute(request);
  }

  @Override
  public Flux<String> stream(HttpRequest request) {
    return Flux.defer(
        () -> {
          check(request.getBody());
          return delegate.stream(request);
        });
  }

  @Override
  public void close() {
    delegate.close();
  }
}
