package com.financeclaw.m2;

import com.financeclaw.application.TaskService;
import com.financeclaw.domain.Task.*;
import com.financeclaw.infrastructure.agentscope.execution.*;
import com.financeclaw.infrastructure.state.RuntimeMapper;
import com.financeclaw.support.PayloadJson;
import io.agentscope.core.message.*;
import io.agentscope.core.model.*;
import java.util.*;
import reactor.core.publisher.Flux;

public final class ScriptedRuntime extends TaskAgentRuntime {
  public ScriptedRuntime(
      TaskService tasks,
      com.financeclaw.application.GovernanceService governance,
      RuntimeMapper states,
      com.financeclaw.infrastructure.agentscope.capability.BusinessCapabilities capabilities) {
    super(tasks, governance, states, capabilities, 15000);
  }

  @Override
  protected Model model(Claim claim) {
    return new Model() {
      public String getModelName() {
        return "m2-scripted";
      }

      public Flux<ChatResponse> stream(
          List<Msg> messages, List<ToolSchema> tools, GenerateOptions options) {
        String name =
            switch (claim.turn().message()) {
              case "city", "city-batch" -> "request_city";
              case "catalog" -> "search_catalog";
              case "mcp" -> "hotels";
              default -> "create_report";
            };
        long resolvedCount =
            messages.stream()
                .flatMap(m -> m.getContentBlocks(ToolResultBlock.class).stream())
                .filter(r -> !r.isSuspended())
                .map(ToolResultBlock::getId)
                .distinct()
                .count();
        boolean batch = Set.of("batch", "city-batch").contains(claim.turn().message());
        boolean resolved = resolvedCount >= (batch ? 2 : 1);
        if (resolved)
          return Flux.just(
              ChatResponse.builder()
                  .id("response")
                  .content(List.of(TextBlock.builder().text("done").build()))
                  .build());
        Map<String, Object> input =
            name.equals("create_report")
                ? Map.of("title", "PRIVATE_REPORT_PARAMETER")
                : name.equals("request_city") ? Map.of() : Map.of("city", "上海");
        var tool =
            ToolUseBlock.builder()
                .id("call-1")
                .name(name)
                .input(input)
                .content(PayloadJson.canonical(input))
                .build();
        return Flux.just(
            ChatResponse.builder()
                .id("response")
                .content(
                    (batch
                        ? List.of(
                            tool,
                            ToolUseBlock.builder()
                                .id("call-2")
                                .name(name)
                                .input(input)
                                .content(PayloadJson.canonical(input))
                                .build())
                        : List.of(
                            TextBlock.builder().text("PRIVATE_INTERMEDIATE_TEXT").build(), tool)))
                .build());
      }
    };
  }
}
