package com.financeclaw.infrastructure.agentscope.execution;

import com.financeclaw.application.ReleaseCatalog.ModelProfile;
import com.financeclaw.domain.Governance.Blocked;
import io.agentscope.core.message.Msg;
import io.agentscope.core.model.*;
import io.agentscope.core.model.transport.*;
import io.agentscope.extensions.model.openai.OpenAIChatModel;
import java.util.List;
import reactor.core.publisher.Flux;

/** Native provider adapter plus a check at its final serialized HTTP request boundary. */
public final class ModelFactory {
  public interface Handle extends Model, AutoCloseable {
    @Override
    void close();
  }

  private ModelFactory() {}

  public static Handle create(ModelProfile profile, String key) {
    return create(
        profile,
        key,
        JdkHttpTransport.builder()
            .config(
                HttpTransportConfig.builder()
                    .connectTimeout(java.time.Duration.ofSeconds(5))
                    .responseTimeout(java.time.Duration.ofSeconds(10))
                    .streamIdleTimeout(java.time.Duration.ofSeconds(10))
                    .build())
            .build());
  }

  public static Handle create(ModelProfile p, String key, HttpTransport transport) {
    var options = GenerateOptions.builder().maxTokens(p.outputReserve());
    if (p.name().toLowerCase(java.util.Locale.ROOT).contains("qwen"))
      options.additionalBodyParam("enable_thinking", p.thinking());
    else if (p.thinking())
      throw new IllegalArgumentException("Thinking profile is only supported for Qwen");
    var model =
        OpenAIChatModel.builder().modelName(p.name()).apiKey(key).baseUrl(p.baseUrl()).stream(true)
            .contextWindowSize(p.window())
            .generateOptions(options.build())
            .httpTransport(
                new BudgetedTransport(
                    transport, p.inputLimit(), p.window(), p.outputReserve(), p.margin()))
            .build();
    return new Handle() {
      @Override
      public void close() {
        transport.close();
      }

      @Override
      public String getModelName() {
        return model.getModelName();
      }

      @Override
      public Flux<ChatResponse> stream(
          List<Msg> messages, List<ToolSchema> tools, GenerateOptions options) {
        return model.stream(messages, tools, options)
            .map(
                response -> {
                  var usage = response.getUsage();
                  if (usage != null
                      && (usage.getInputTokens() < 0
                          || usage.getOutputTokens() < 0
                          || usage.getOutputTokens() > p.outputReserve()
                          || (long) usage.getInputTokens() + usage.getOutputTokens() > p.window()))
                    throw new Blocked("invalid_model_usage");
                  return response;
                });
      }
    };
  }
}
