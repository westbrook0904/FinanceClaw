package com.financeclaw.infrastructure.agentscope.execution;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import io.agentscope.core.model.transport.HttpRequest;
import io.agentscope.core.model.transport.HttpResponse;
import io.agentscope.core.model.transport.HttpTransport;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import reactor.core.publisher.Flux;

/**
 * 对 SDK 已序列化的最终模型请求执行预算检查。
 *
 * <p>计入 system、历史、工具 schema、输出 schema 与 JSON 封装；画像和当前消息不能 通过只截历史绕过总预算。没有模型 tokenizer 时使用保守 UTF-8
 * 字节上界，未定价的 多模态输入显式拒绝。流式请求在订阅时校验，重试或备用模型同样要经过各自预算。
 */
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
        if (inputLimit < 1 || contextWindow < 1 || outputReserve < 1 || safetyMargin < 0) {
            throw new IllegalArgumentException("Invalid model budget");
        }
        this.delegate = delegate;
        this.outputReserve = outputReserve;
        this.capacity = Math.min(inputLimit, contextWindow - outputReserve) - safetyMargin;
        if (capacity < 1) {
            throw new IllegalArgumentException("No input capacity remains");
        }
    }

    public int check(String body) {
        if (body == null) {
            throw new IllegalArgumentException("Missing model request body");
        }
        try {
            JsonNode request = new ObjectMapper().readTree(body);
            if (!request.isObject() || !request.path("messages").isArray()) {
                throw new IllegalArgumentException("Unsupported model request shape");
            }
            rejectUnpricedMedia(request.path("messages"));
            int output =
                    request.path("max_completion_tokens")
                            .asInt(request.path("max_tokens").asInt(-1));
            if (output < 1 || output > outputReserve) {
                throw new IllegalArgumentException(
                        "Model output exceeds reserved capacity or is unspecified");
            }
        } catch (IOException error) {
            throw new IllegalArgumentException("Invalid model JSON", error);
        }
        int estimatedTokens = body.getBytes(StandardCharsets.UTF_8).length;
        if (estimatedTokens > capacity) {
            throw new IllegalArgumentException(
                    "Model input exceeds UTF-8 fallback budget: "
                            + estimatedTokens
                            + " > "
                            + capacity);
        }
        return estimatedTokens;
    }

    private static void rejectUnpricedMedia(JsonNode node) {
        if (node.isObject()) {
            String type = node.path("type").asText();
            if (type.equals("image_url")
                    || type.equals("input_audio")
                    || type.equals("video_url")
                    || type.equals("file")) {
                throw new IllegalArgumentException(
                        "Multimodal input requires an explicit modality budget");
            }
        }
        if (node.isContainerNode()) {
            node.forEach(BudgetedTransport::rejectUnpricedMedia);
        }
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
