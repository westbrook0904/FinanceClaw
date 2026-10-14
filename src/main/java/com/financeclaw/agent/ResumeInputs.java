package com.financeclaw.agent;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.SerializationFeature;
import com.fasterxml.jackson.databind.json.JsonMapper;
import com.financeclaw.domain.ExecutionScope;
import io.agentscope.core.event.ConfirmResult;
import io.agentscope.core.message.*;
import io.agentscope.core.state.AgentState;
import io.agentscope.core.state.AgentStateStore;
import java.util.List;
import java.util.Map;

/** Build SDK resume metadata from saved state, never from a client-supplied ToolUseBlock. */
public final class ResumeInputs {
  private ResumeInputs() {}

  public static String actionHash(ToolUseBlock tool) {
    return ExecutionScope.hash(List.of(tool.getId(), tool.getName(), canonical(tool.getInput())));
  }

  public static String canonical(Object input) {
    try {
      return JsonMapper.builder()
          .enable(SerializationFeature.ORDER_MAP_ENTRIES_BY_KEYS)
          .build()
          .writeValueAsString(input);
    } catch (JsonProcessingException e) {
      throw new IllegalArgumentException("Invalid action payload", e);
    }
  }

  public static Msg approval(
      AgentStateStore store,
      ExecutionScope scope,
      long version,
      String callId,
      String actionHash,
      boolean approved) {
    ToolUseBlock tool = pending(store, scope, version, callId);
    if (tool.getState() != ToolCallState.ASKING || !actionHash(tool).equals(actionHash)) {
      throw new IllegalArgumentException("Approval action does not match the saved question");
    }
    return Msg.builder()
        .name("user")
        .role(MsgRole.USER)
        .textContent(approved ? "approved" : "denied")
        .metadata(Map.of(Msg.METADATA_CONFIRM_RESULTS, List.of(new ConfirmResult(approved, tool))))
        .build();
  }

  public static Msg clarification(
      AgentStateStore store,
      ExecutionScope scope,
      long version,
      String callId,
      String actionHash,
      String city) {
    ToolUseBlock tool = pending(store, scope, version, callId);
    if (!tool.getName().equals("request_city")
        || tool.getState() == ToolCallState.ASKING
        || !actionHash(tool).equals(actionHash)
        || city == null
        || city.isBlank()
        || city.length() > 100) {
      throw new IllegalArgumentException("Invalid answer to the saved city question");
    }
    return Msg.builder()
        .name("tool")
        .role(MsgRole.TOOL)
        .content(
            ToolResultBlock.of(
                tool.getId(),
                tool.getName(),
                TextBlock.builder().text(canonical(Map.of("city", city))).build()))
        .build();
  }

  public static ToolUseBlock pending(
      AgentStateStore store, ExecutionScope scope, long version, String callId) {
    var saved =
        store.getVersioned(scope.userId(), scope.sessionId(), "agent_state", AgentState.class);
    if (!saved.isPresent() || saved.version() != version)
      throw new IllegalArgumentException("Stale interaction state version");
    var messages = saved.value().getContext();
    boolean resolved =
        messages.stream()
            .flatMap(m -> m.getContentBlocks(ToolResultBlock.class).stream())
            .anyMatch(result -> callId.equals(result.getId()) && !result.isSuspended());
    if (resolved) throw new IllegalArgumentException("Tool call already resolved");
    return messages.stream()
        .flatMap(m -> m.getContentBlocks(ToolUseBlock.class).stream())
        .filter(tool -> callId.equals(tool.getId()))
        .findFirst()
        .orElseThrow(() -> new IllegalArgumentException("Question does not belong to this state"));
  }
}
