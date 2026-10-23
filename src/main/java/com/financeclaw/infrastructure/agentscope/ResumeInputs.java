package com.financeclaw.infrastructure.agentscope;

import com.financeclaw.domain.ExecutionScope;
import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.support.PayloadJson;
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
        return ExecutionScope.hash(
                List.of(tool.getId(), tool.getName(), PayloadJson.canonical(tool.getInput())));
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
                .metadata(
                        Map.of(
                                Msg.METADATA_CONFIRM_RESULTS,
                                List.of(new ConfirmResult(approved, tool))))
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
        if (!actionHash(tool).equals(actionHash)) {
            throw new IllegalArgumentException("Invalid answer to the saved city question");
        }
        return clarification(tool, city);
    }

    public static Msg clarification(ToolUseBlock tool, String city) {
        if (!tool.getName().equals("request_city")
                || tool.getState() == ToolCallState.ASKING
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
                                        TextBlock.builder()
                                                .text(PayloadJson.canonical(Map.of("city", city)))
                                                .build())
                                .withState(ToolResultState.SUCCESS))
                .build();
    }

    public static Msg result(ToolUseBlock call, ToolResultBlock result) {
        if (result == null || result.isSuspended()) {
            throw new Blocked("tool_execution_failed");
        }
        // SDK 2.0.3 text()/of() return unbound RUNNING values; ReAct normally finalizes them
        // after the tool Mono completes. A saved, call-bound RUNNING result is still unfinished.
        boolean rawReturn =
                result.getId() == null
                        && result.getName() == null
                        && result.getState() == ToolResultState.RUNNING;
        boolean errorMarker =
                result.getOutput().stream()
                        .anyMatch(
                                block ->
                                        block instanceof TextBlock text
                                                && text.getText() != null
                                                && text.getText().startsWith("[ERROR]"));
        if (result.getState() != ToolResultState.SUCCESS && !(rawReturn && !errorMarker)) {
            throw new Blocked("tool_execution_failed");
        }
        return Msg.builder()
                .name("tool")
                .role(MsgRole.TOOL)
                .content(
                        ToolResultBlock.of(call.getId(), call.getName(), result.getOutput())
                                .withState(ToolResultState.SUCCESS))
                .build();
    }

    /** 从绑定 scope 读取真实检查点并核对版本，再定位仍未解决的 callId。 任何旧答案、已解决调用或其他执行状态都不得作为本次恢复输入。 */
    public static ToolUseBlock pending(
            AgentStateStore store, ExecutionScope scope, long version, String callId) {
        var saved =
                store.getVersioned(
                        scope.userId(), scope.sessionId(), "agent_state", AgentState.class);
        if (!saved.isPresent() || saved.version() != version) {
            throw new IllegalArgumentException("Stale interaction state version");
        }
        return new AgentStateInspector(saved.value()).pending(callId);
    }
}
