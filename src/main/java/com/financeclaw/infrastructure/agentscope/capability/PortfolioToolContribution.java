package com.financeclaw.infrastructure.agentscope.capability;

import com.financeclaw.app.portfolio.PortfolioReviewService;
import com.financeclaw.domain.Task.Claim;
import com.financeclaw.support.PayloadJson;
import io.agentscope.core.message.ToolResultBlock;
import reactor.core.publisher.Mono;

public final class PortfolioToolContribution implements ToolContribution {
  private final PortfolioReviewService workflow;
  private final Claim claim;
  private final boolean enabled;

  public PortfolioToolContribution(PortfolioReviewService workflow, Claim claim, boolean enabled) {
    this.workflow = workflow;
    this.claim = claim;
    this.enabled = enabled;
  }

  public void contribute(Registrar registrar) {
    registrar.tool(
        "portfolio_review",
        PortfolioInputs.SCHEMA,
        "workflow:run",
        p ->
            Mono.fromCallable(
                () ->
                    ToolResultBlock.text(
                        PayloadJson.canonical(
                            workflow.prepare(claim, PortfolioInputs.parse(p.getInput()))))));
    if (enabled)
      registrar.tool(
          "publish_portfolio_report",
          PortfolioInputs.PUBLISH,
          "report:write",
          p ->
              Mono.fromCallable(
                  () ->
                      ToolResultBlock.text(
                          PayloadJson.canonical(
                              workflow.publish(claim, (String) p.getInput().get("draft_hash"))))),
          new ApprovalInteractionHandler(call -> "是否发布投资组合复盘报告？"));
  }
}
