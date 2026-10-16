package com.financeclaw.infrastructure.agentscope;

import com.financeclaw.domain.Task.Outcome;
import io.agentscope.core.message.*;
import io.agentscope.core.state.AgentState;
import java.util.*;

/** One interpretation of persisted SDK calls, used by outcomes, questions and answer binding. */
public final class AgentStateInspector {
  private final Map<String, ToolUseBlock> calls = new LinkedHashMap<>();
  private final Map<String, ToolResultBlock> results = new LinkedHashMap<>();
  private final AgentState state;

  public AgentStateInspector(AgentState state) {
    this.state = state;
    if (state != null)
      for (var message : state.getContext()) {
        message.getContentBlocks(ToolUseBlock.class).forEach(call -> calls.put(call.getId(), call));
        message
            .getContentBlocks(ToolResultBlock.class)
            .forEach(result -> results.put(result.getId(), result));
      }
  }

  public List<ToolUseBlock> pending() {
    if (state == null || state.isShutdownInterrupted()) return List.of();
    return calls.values().stream().filter(this::unresolved).toList();
  }

  public ToolUseBlock pending(String id) {
    var call = calls.get(id);
    if (call == null) throw new IllegalArgumentException("Question does not belong to this state");
    if (state.isShutdownInterrupted() || !unresolved(call))
      throw new IllegalArgumentException("Tool call already resolved");
    return call;
  }

  private boolean unresolved(ToolUseBlock call) {
    var result = results.get(call.getId());
    if (result != null && failed(result)) return false;
    return result == null
        || result.isSuspended()
        || result.getState() == ToolResultState.RUNNING
        || (call.getState() == ToolCallState.ASKING
            && result.getState() == ToolResultState.SUCCESS);
  }

  private static boolean failed(ToolResultBlock result) {
    return result.getState() == ToolResultState.ERROR
        || result.getState() == ToolResultState.INTERRUPTED
        || result.getState() == ToolResultState.DENIED;
  }

  public static Outcome classify(AgentState state, long version) {
    return new AgentStateInspector(state).outcome(version);
  }

  private Outcome outcome(long version) {
    if (state == null || state.isShutdownInterrupted()) return new Outcome("failed", null, version);
    if (!pending().isEmpty()) return new Outcome("waiting", null, version);
    if (results.values().stream().anyMatch(AgentStateInspector::failed))
      return new Outcome("failed", null, version);
    if (state.getContext().isEmpty()) return new Outcome("failed", null, version);
    var last = state.getContext().getLast();
    var text = last.getTextContent();
    if (last.getRole() != MsgRole.ASSISTANT || text == null || text.isBlank())
      return new Outcome("failed", null, version);
    return new Outcome("completed", text, version);
  }
}
