package com.financeclaw.agent;

import com.financeclaw.domain.ExecutionScope;
import io.agentscope.core.ReActAgent;
import io.agentscope.core.agent.RuntimeContext;
import io.agentscope.core.model.Model;
import io.agentscope.core.permission.PermissionContextState;
import io.agentscope.core.permission.PermissionMode;
import io.agentscope.core.state.AgentStateStore;
import io.agentscope.core.state.ConflictPolicy;
import io.agentscope.core.tool.ToolBase;
import io.agentscope.core.tool.Toolkit;

/** Create an engine/toolkit per execution; never share mutable SDK caches across tenants. */
public final class IsolatedAgentFactory {
  private IsolatedAgentFactory() {}

  public static ReActAgent create(
      Model model, Toolkit toolkit, AgentStateStore store, PermissionContextState permissions) {
    if (permissions == null
        || permissions.isTrivial()
        || permissions.getMode() == PermissionMode.BYPASS) {
      throw new IllegalArgumentException("An explicit non-bypass permission policy is required");
    }
    for (String name : toolkit.getToolNames()) {
      if (!(toolkit.getTool(name) instanceof ToolBase)) {
        throw new IllegalArgumentException(
            "Tool must extend ToolBase for permission evaluation: " + name);
      }
    }
    return ReActAgent.builder()
        .name("finance_agent")
        .sysPrompt("Use only the tools authorized for this task.")
        .model(model)
        .toolkit(toolkit)
        .stateStore(store)
        .permissionContext(permissions)
        .conflictPolicy(ConflictPolicy.FAIL)
        .enablePendingToolRecovery(false)
        // In 2.0.3 this maps to ExecutionConfig.maxAttempts; one disables retry.
        .maxRetries(1)
        .maxIters(6)
        .build();
  }

  public static RuntimeContext context(ExecutionScope scope) {
    return RuntimeContext.builder().userId(scope.userId()).sessionId(scope.sessionId()).build();
  }
}
