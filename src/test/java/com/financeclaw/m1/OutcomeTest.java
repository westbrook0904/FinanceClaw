package com.financeclaw.m1;

import static org.assertj.core.api.Assertions.assertThat;

import com.financeclaw.infrastructure.agentscope.execution.TaskAgentRuntime;
import io.agentscope.core.message.*;
import io.agentscope.core.state.AgentState;
import java.util.Map;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.CsvSource;

class OutcomeTest {
    @ParameterizedTest
    @CsvSource({
        "ASKING,SUCCESS,waiting",
        "SUBMITTED,RUNNING,waiting",
        "FINISHED,ERROR,failed",
        "FINISHED,INTERRUPTED,failed",
        "FINISHED,DENIED,failed",
        "FINISHED,SUCCESS,completed"
    })
    void toolStateTakesPrecedenceOverFinalLookingText(
            ToolCallState call, ToolResultState result, String expected) {
        var use =
                ToolUseBlock.builder()
                        .id("tool-1")
                        .name("read")
                        .input(Map.of())
                        .content("{}")
                        .state(call)
                        .build();
        var reply = ToolResultBlock.text("result").withState(result);
        var state =
                AgentState.builder()
                        .addMessage(Msg.builder().role(MsgRole.ASSISTANT).content(use).build())
                        .addMessage(
                                Msg.builder()
                                        .role(MsgRole.TOOL)
                                        .content(
                                                new ToolResultBlock(
                                                        "tool-1",
                                                        "read",
                                                        reply.getOutput(),
                                                        reply.getMetadata(),
                                                        reply.getState()))
                                        .build())
                        .addMessage(
                                Msg.builder()
                                        .role(MsgRole.ASSISTANT)
                                        .content(TextBlock.builder().text("Done").build())
                                        .build())
                        .build();
        assertThat(TaskAgentRuntime.classify(state, 1).status()).isEqualTo(expected);
    }
}
