package com.financeclaw.m2;

import com.financeclaw.agent.*;
import com.financeclaw.application.TaskService;
import com.financeclaw.domain.Task.*;
import com.financeclaw.infrastructure.state.RuntimeMapper;
import io.agentscope.core.message.*;
import io.agentscope.core.model.*;
import java.util.*;
import reactor.core.publisher.Flux;

final class ScriptedRuntime extends TaskAgentRuntime {
  ScriptedRuntime(
      TaskService tasks,
      com.financeclaw.application.GovernanceService governance,
      RuntimeMapper states) {
    super(tasks, governance, states, 15000);
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
              case "city" -> "request_city";
              case "catalog" -> "search_catalog";
              case "mcp" -> "mcp_lookup";
              default -> "create_report";
            };
        long resolvedCount =
            messages.stream()
                .flatMap(m -> m.getContentBlocks(ToolResultBlock.class).stream())
                .filter(r -> !r.isSuspended())
                .map(ToolResultBlock::getId)
                .distinct()
                .count();
        boolean resolved = resolvedCount >= (claim.turn().message().equals("batch") ? 2 : 1);
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
                .content(ResumeInputs.canonical(input))
                .build();
        return Flux.just(
            ChatResponse.builder()
                .id("response")
                .content(
                    (claim.turn().message().equals("batch")
                        ? List.of(
                            tool,
                            ToolUseBlock.builder()
                                .id("call-2")
                                .name(name)
                                .input(input)
                                .content(ResumeInputs.canonical(input))
                                .build())
                        : List.of(
                            TextBlock.builder().text("PRIVATE_INTERMEDIATE_TEXT").build(), tool)))
                .build());
      }
    };
  }
}
