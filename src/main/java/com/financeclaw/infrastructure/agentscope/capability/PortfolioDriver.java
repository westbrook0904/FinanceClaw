package com.financeclaw.infrastructure.agentscope.capability;

import com.financeclaw.app.portfolio.PortfolioReviewService;
import com.financeclaw.domain.Task.Claim;
import com.financeclaw.infrastructure.agentscope.NativeToolCalls;
import com.financeclaw.support.PayloadJson;
import io.agentscope.core.message.*;
import io.agentscope.core.model.*;
import java.util.*;
import reactor.core.publisher.Flux;

/** Native ReAct supplies scheduling and ASK/resume; this Model chooses the fixed business steps. */
public final class PortfolioDriver implements Model {
  private final PortfolioReviewService workflow;
  private final Claim claim;
  private final Map<String, Object> input;

  public PortfolioDriver(PortfolioReviewService workflow, Claim claim) {
    this.workflow = workflow;
    this.claim = claim;
    this.input =
        PortfolioInputs.parse(
                PayloadJson.object(
                    claim.turn().message().substring(PortfolioReviewService.COMMAND.length())))
            .payload();
  }

  public String getModelName() {
    return "portfolio-fixed-schedule";
  }

  public Flux<ChatResponse> stream(
      List<Msg> messages, List<ToolSchema> tools, GenerateOptions options) {
    List<ToolResultBlock> results =
        messages.stream()
            .flatMap(m -> m.getContentBlocks(ToolResultBlock.class).stream())
            .filter(r -> !r.isSuspended())
            .toList();
    ContentBlock output;
    if (results.stream()
        .anyMatch(
            r -> r.getState() == ToolResultState.ERROR || r.getState() == ToolResultState.DENIED))
      output = TextBlock.builder().text("组合复盘未发布：步骤失败或审批被拒绝。").build();
    else if (results.stream().anyMatch(r -> r.getName().equals("publish_portfolio_report")))
      output =
          TextBlock.builder()
              .text(
                  results.getLast().getOutput().stream()
                      .filter(b -> b instanceof TextBlock)
                      .map(b -> ((TextBlock) b).getText())
                      .findFirst()
                      .orElseThrow())
              .build();
    else if (results.stream().anyMatch(r -> r.getName().equals("portfolio_review")))
      output =
          NativeToolCalls.call(
              "publish",
              "publish_portfolio_report",
              Map.of("draft_hash", workflow.preparedHash(claim)));
    else output = NativeToolCalls.call("prepare", "portfolio_review", input);
    return Flux.just(ChatResponse.builder().id("workflow").content(List.of(output)).build());
  }
}
