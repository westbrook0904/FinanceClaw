package com.financeclaw.infrastructure.agentscope.capability;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.Mockito.*;

import com.financeclaw.app.portfolio.PortfolioReviewService;
import com.financeclaw.domain.Task.*;
import com.financeclaw.infrastructure.agentscope.context.WorkingContextSession;
import com.financeclaw.support.PayloadJson;
import io.agentscope.core.message.*;
import io.agentscope.core.model.GenerateOptions;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;

class PortfolioDriverContextTest {
    private final PortfolioReviewService workflow = mock(PortfolioReviewService.class);
    private final Claim claim =
            new Claim(
                    new Command("command", "turn", "running", "attempt", 1, true, null),
                    new Turn(
                            "turn",
                            "conversation",
                            "tenant",
                            "subject",
                            "key",
                            "request-hash",
                            PortfolioReviewService.COMMAND
                                    + PayloadJson.canonical(
                                            Map.of(
                                                    "portfolio_name",
                                                    "本轮组合",
                                                    "positions",
                                                    List.of(
                                                            Map.of(
                                                                    "symbol",
                                                                    "AAPL",
                                                                    "quantity",
                                                                    1,
                                                                    "cost_basis",
                                                                    100)))),
                            "release",
                            "running",
                            1,
                            null,
                            null));

    @Test
    void oldPublishedReportCannotCompleteNewTurnOrSkipItsPrepareAndApproval() {
        var result = run(history("publish_portfolio_report", ToolResultState.SUCCESS));
        assertThat(result).isInstanceOf(ToolUseBlock.class);
        assertThat(((ToolUseBlock) result).getName()).isEqualTo("portfolio_review");
        verifyNoInteractions(workflow);
    }

    @Test
    void historicalFailureCannotFailANewPortfolioTurn() {
        var result = run(history("portfolio_review", ToolResultState.ERROR));
        assertThat(result).isInstanceOf(ToolUseBlock.class);
        assertThat(((ToolUseBlock) result).getName()).isEqualTo("portfolio_review");
    }

    @Test
    void currentPreparedResultStillUsesCurrentDraftForPublicationApproval() {
        when(workflow.preparedHash(claim)).thenReturn("a".repeat(64));
        var current =
                Msg.builder()
                        .role(MsgRole.TOOL)
                        .content(
                                new ToolResultBlock(
                                        "prepare",
                                        "portfolio_review",
                                        List.of(TextBlock.builder().text("draft").build()),
                                        Map.of(),
                                        ToolResultState.SUCCESS))
                        .build();
        var result =
                new PortfolioDriver(workflow, claim)
                        .stream(
                                        List.of(
                                                history(
                                                        "publish_portfolio_report",
                                                        ToolResultState.SUCCESS),
                                                current),
                                        List.of(),
                                        GenerateOptions.builder().build())
                                .blockLast()
                                .getContent()
                                .getFirst();
        assertThat(result).isInstanceOf(ToolUseBlock.class);
        assertThat(((ToolUseBlock) result).getName()).isEqualTo("publish_portfolio_report");
        assertThat(((ToolUseBlock) result).getInput()).containsEntry("draft_hash", "a".repeat(64));
        verify(workflow).preparedHash(claim);
    }

    private ContentBlock run(Msg old) {
        return new PortfolioDriver(workflow, claim)
                .stream(
                                List.of(
                                        old,
                                        Msg.builder()
                                                .id("turn-input-turn")
                                                .role(MsgRole.USER)
                                                .textContent(claim.turn().message())
                                                .build()),
                                List.of(),
                                GenerateOptions.builder().build())
                        .blockLast()
                        .getContent()
                        .getFirst();
    }

    private static Msg history(String name, ToolResultState state) {
        return Msg.builder()
                .role(MsgRole.TOOL)
                .metadata(Map.of(WorkingContextSession.HISTORY_MARKER, true))
                .content(
                        new ToolResultBlock(
                                "old-call",
                                name,
                                List.of(
                                        TextBlock.builder()
                                                .text("OLD_REPORT_MUST_NOT_RETURN")
                                                .build()),
                                Map.of(),
                                state))
                .build();
    }
}
