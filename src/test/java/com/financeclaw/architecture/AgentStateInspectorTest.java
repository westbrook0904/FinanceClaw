package com.financeclaw.architecture;

import static org.assertj.core.api.Assertions.*;

import com.financeclaw.domain.ExecutionScope;
import com.financeclaw.infrastructure.agentscope.*;
import io.agentscope.core.message.*;
import io.agentscope.core.state.*;
import java.util.*;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.*;

class AgentStateInspectorTest {
  private final ExecutionScope scope = new ExecutionScope("t", "s", "c", "agent", "release");

  @ParameterizedTest
  @CsvSource({
    "ASKING,SUCCESS,true",
    "SUBMITTED,RUNNING,true",
    "FINISHED,SUCCESS,false",
    "FINISHED,ERROR,false",
    "FINISHED,INTERRUPTED,false",
    "ASKING,DENIED,false"
  })
  void outcomeAndResumeInterpretTheSameSavedCall(
      ToolCallState callState, ToolResultState resultState, boolean pending) {
    var call = call("one", callState);
    var snapshot = state(call, result(call, resultState));
    var inspector = new AgentStateInspector(snapshot);
    var store = new InMemoryAgentStateStore();
    long version =
        store.saveIfVersion(scope.userId(), scope.sessionId(), "agent_state", snapshot, 0);
    assertThat(inspector.pending().isEmpty()).isEqualTo(!pending);
    assertThat(AgentStateInspector.classify(snapshot, version).status().equals("waiting"))
        .isEqualTo(pending);
    if (pending) assertThat(ResumeInputs.pending(store, scope, version, "one")).isEqualTo(call);
    else
      assertThatThrownBy(() -> ResumeInputs.pending(store, scope, version, "one"))
          .isInstanceOf(IllegalArgumentException.class)
          .hasMessage("Tool call already resolved");
  }

  @Test
  void suspendedAndMissingResultsRemainPendingInStableCallOrder() {
    var first = call("first", ToolCallState.SUBMITTED);
    var second = call("second", ToolCallState.ASKING);
    var snapshot =
        AgentState.builder()
            .addMessage(
                Msg.builder().role(MsgRole.ASSISTANT).content(List.of(first, second)).build())
            .addMessage(
                Msg.builder().role(MsgRole.TOOL).content(ToolResultBlock.suspended(first)).build())
            .build();
    assertThat(new AgentStateInspector(snapshot).pending()).containsExactly(first, second);
  }

  @Test
  void latestCallAndLatestResultWinAndEachCallProducesOneQuestion() {
    var first = call("one", ToolCallState.ASKING);
    var updated = call("one", ToolCallState.FINISHED);
    var snapshot =
        AgentState.builder()
            .addMessage(Msg.builder().role(MsgRole.ASSISTANT).content(first).build())
            .addMessage(
                Msg.builder().role(MsgRole.TOOL).content(ToolResultBlock.suspended(first)).build())
            .addMessage(Msg.builder().role(MsgRole.ASSISTANT).content(updated).build())
            .addMessage(
                Msg.builder()
                    .role(MsgRole.TOOL)
                    .content(result(updated, ToolResultState.SUCCESS))
                    .build())
            .addMessage(Msg.builder().role(MsgRole.ASSISTANT).textContent("done").build())
            .build();
    assertThat(new AgentStateInspector(snapshot).pending()).isEmpty();
    assertThat(AgentStateInspector.classify(snapshot, 4).status()).isEqualTo("completed");
    var duplicate =
        AgentState.builder()
            .addMessage(
                Msg.builder().role(MsgRole.ASSISTANT).content(List.of(first, first)).build())
            .build();
    assertThat(new AgentStateInspector(duplicate).pending()).containsExactly(first);
  }

  @Test
  void missingVersionUnknownCallAndInterruptedSnapshotCannotBeAnswered() {
    var store = new InMemoryAgentStateStore();
    var call = call("one", ToolCallState.ASKING);
    assertThatThrownBy(() -> ResumeInputs.pending(store, scope, 1, "one"))
        .hasMessage("Stale interaction state version");
    store.saveIfVersion(scope.userId(), scope.sessionId(), "agent_state", state(call, null), 0);
    assertThatThrownBy(() -> ResumeInputs.pending(store, scope, 2, "one"))
        .hasMessage("Stale interaction state version");
    assertThatThrownBy(() -> ResumeInputs.pending(store, scope, 1, "other"))
        .hasMessage("Question does not belong to this state");
    var interrupted = state(call, null);
    interrupted.setShutdownInterrupted(true);
    store.saveIfVersion(scope.userId(), scope.sessionId(), "agent_state", interrupted, 1);
    assertThat(new AgentStateInspector(interrupted).pending()).isEmpty();
    assertThat(AgentStateInspector.classify(interrupted, 2).status()).isEqualTo("failed");
    assertThatThrownBy(() -> ResumeInputs.pending(store, scope, 2, "one"))
        .hasMessage("Tool call already resolved");
  }

  @ParameterizedTest
  @EnumSource(
      value = ToolResultState.class,
      names = {"ERROR", "DENIED", "INTERRUPTED", "RUNNING"})
  void resumeNeverRewritesAnUnsuccessfulBusinessResultAsSuccess(ToolResultState state) {
    var call = call("one", ToolCallState.SUBMITTED);
    assertThatThrownBy(() -> ResumeInputs.result(call, result(call, state)))
        .hasMessage("tool_execution_failed");
  }

  @Test
  void suspendedResultCannotBeReportedAsSuccessfulResume() {
    var call = call("one", ToolCallState.SUBMITTED);
    assertThatThrownBy(() -> ResumeInputs.result(call, ToolResultBlock.suspended(call)))
        .hasMessage("tool_execution_failed");
    assertThat(
            ResumeInputs.result(call, result(call, ToolResultState.SUCCESS))
                .getContentBlocks(ToolResultBlock.class)
                .getFirst()
                .getId())
        .isEqualTo("one");
  }

  @Test
  void completedNativeToolReturnIsFinalizedWithoutTreatingSavedRunningResultsAsSuccess() {
    var call = call("one", ToolCallState.SUBMITTED);
    var raw = ToolResultBlock.text("done");
    assertThat(raw.getState()).isEqualTo(ToolResultState.RUNNING);
    assertThat(raw.getId()).isNull();
    var message = ResumeInputs.result(call, raw).getContentBlocks(ToolResultBlock.class).getFirst();
    assertThat(message.getId()).isEqualTo("one");
    assertThat(message.getState()).isEqualTo(ToolResultState.SUCCESS);
    assertThatThrownBy(() -> ResumeInputs.result(call, ToolResultBlock.text("[ERROR] failed")))
        .hasMessage("tool_execution_failed");
    assertThatThrownBy(() -> ResumeInputs.result(call, ToolResultBlock.error("failed")))
        .hasMessage("tool_execution_failed");
  }

  @ParameterizedTest
  @EnumSource(
      value = ToolResultState.class,
      names = {"ERROR", "DENIED", "INTERRUPTED"})
  void terminalFailureWinsOverStaleSuspendedMetadata(ToolResultState resultState) {
    var call = call("one", ToolCallState.ASKING);
    var snapshot = state(call, ToolResultBlock.suspended(call).withState(resultState));
    assertThat(new AgentStateInspector(snapshot).pending()).isEmpty();
    assertThat(AgentStateInspector.classify(snapshot, 1).status()).isEqualTo("failed");
    assertThatThrownBy(() -> new AgentStateInspector(snapshot).pending("one"))
        .hasMessage("Tool call already resolved");
  }

  private ToolUseBlock call(String id, ToolCallState state) {
    return ToolUseBlock.builder()
        .id(id)
        .name("create_report")
        .input(Map.of("title", "test"))
        .content("{\"title\":\"test\"}")
        .state(state)
        .build();
  }

  private ToolResultBlock result(ToolUseBlock call, ToolResultState state) {
    return ToolResultBlock.of(
            call.getId(), call.getName(), TextBlock.builder().text("result").build())
        .withState(state);
  }

  private AgentState state(ToolUseBlock call, ToolResultBlock result) {
    var builder =
        AgentState.builder()
            .addMessage(Msg.builder().role(MsgRole.ASSISTANT).content(call).build());
    if (result != null)
      builder.addMessage(Msg.builder().role(MsgRole.TOOL).content(result).build());
    return builder
        .addMessage(Msg.builder().role(MsgRole.ASSISTANT).textContent("done").build())
        .build();
  }
}
