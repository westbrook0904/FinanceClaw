package com.financeclaw.m2;

import static org.assertj.core.api.Assertions.*;
import static org.mockito.Mockito.*;

import com.financeclaw.domain.Task.*;
import com.financeclaw.fixture.GovernanceFixture;
import com.financeclaw.fixture.TaskFixture;
import com.financeclaw.infrastructure.agentscope.capability.BusinessCapabilities;
import com.financeclaw.infrastructure.agentscope.context.WorkingContextSession;
import com.financeclaw.infrastructure.state.RuntimeMapper;
import io.agentscope.core.message.*;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;

/** 飞书恢复场景使用的固定模型必须区分历史工具回执和当前 Turn，不能因前一轮成功而跳过新审批。 */
class FeishuScriptedModelTest {
    private final ScriptedRuntime runtime =
            new ScriptedRuntime(
                    mock(TaskFixture.class),
                    mock(GovernanceFixture.class),
                    mock(RuntimeMapper.class),
                    mock(BusinessCapabilities.class));

    @Test
    void completedClarificationFromThePreviousTurnCannotSkipANewReportApproval() {
        var result =
                model(
                        "write",
                        List.of(receipt("historic-call", "request_city", true), user("write")));
        assertThat(result.getContent())
                .anySatisfy(
                        block -> {
                            assertThat(block)
                                    .isInstanceOfSatisfying(
                                            ToolUseBlock.class,
                                            call ->
                                                    assertThat(call.getName())
                                                            .isEqualTo("create_report"));
                        });
        assertThat(result.getContent().stream().filter(ToolUseBlock.class::isInstance)).hasSize(1);
    }

    @Test
    void currentReceiptStillCompletesTheResumedReportWithHistoricalContextPresent() {
        var result =
                model(
                        "write",
                        List.of(
                                receipt("historic-call", "request_city", true),
                                user("write"),
                                receipt("call-1", "create_report", false)));
        assertThat(result.getContent()).hasSize(1);
        assertThat(result.getContent().getFirst())
                .isInstanceOfSatisfying(
                        TextBlock.class, text -> assertThat(text.getText()).isEqualTo("done"));
    }

    @Test
    void historicalReceiptsCannotSupplyMissingCurrentBatchResults() {
        var result =
                model(
                        "batch",
                        List.of(
                                receipt("historic-1", "request_city", true),
                                receipt("historic-2", "create_report", true),
                                user("batch"),
                                receipt("call-1", "create_report", false)));
        assertThat(result.getContent())
                .hasSize(2)
                .allSatisfy(block -> assertThat(block).isInstanceOf(ToolUseBlock.class));
    }

    private io.agentscope.core.model.ChatResponse model(String message, List<Msg> context) {
        var claim =
                new Claim(
                        new Command("command", "turn", "running", "attempt", 1, true, null),
                        new Turn(
                                "turn",
                                "conversation",
                                "tenant",
                                "subject",
                                "key",
                                "hash",
                                message,
                                "release",
                                "running",
                                1,
                                null,
                                null));
        return runtime.model(claim).stream(context, List.of(), null).blockFirst();
    }

    private static Msg user(String text) {
        return Msg.builder().id("turn-input-turn").role(MsgRole.USER).textContent(text).build();
    }

    private static Msg receipt(String id, String tool, boolean historical) {
        return Msg.builder()
                .role(MsgRole.TOOL)
                .name(tool)
                .content(ToolResultBlock.text("done").withIdAndName(id, tool))
                .metadata(
                        historical ? Map.of(WorkingContextSession.HISTORY_MARKER, true) : Map.of())
                .build();
    }
}
