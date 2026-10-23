package com.financeclaw.infrastructure.agentscope;

import com.financeclaw.domain.ExecutionScope;
import io.agentscope.core.ReActAgent;
import io.agentscope.core.agent.RuntimeContext;
import io.agentscope.core.middleware.FinalAnswerFilterMiddleware;
import io.agentscope.core.model.Model;
import io.agentscope.core.permission.PermissionContextState;
import io.agentscope.core.permission.PermissionMode;
import io.agentscope.core.state.AgentStateStore;
import io.agentscope.core.state.ConflictPolicy;
import io.agentscope.core.tool.ToolBase;
import io.agentscope.core.tool.Toolkit;

/**
 * 为每次执行创建独立 Agent、Toolkit 和可变权限上下文。
 *
 * <p>不跨 owner 复用 SDK 的工具缓存或挂起调用。必须提供非 BYPASS 权限策略，所有 工具继承 ToolBase 才能进入框架的权限流程。ConflictPolicy.FAIL
 * 让冲突显式失败； 关闭 SDK 自动恢复未知工具及自动重试，恢复与重试由持久业务边界决定。
 */
public final class IsolatedAgentFactory {
    private IsolatedAgentFactory() {}

    public static ReActAgent create(
            Model model,
            Toolkit toolkit,
            AgentStateStore store,
            PermissionContextState permissions) {
        return create(model, null, toolkit, store, permissions);
    }

    public static ReActAgent create(
            Model model,
            Model fallback,
            Toolkit toolkit,
            AgentStateStore store,
            PermissionContextState permissions) {
        return create(model, fallback, toolkit, store, permissions, builder -> {});
    }

    public static ReActAgent create(
            Model model,
            Model fallback,
            Toolkit toolkit,
            AgentStateStore store,
            PermissionContextState permissions,
            java.util.function.Consumer<ReActAgent.Builder> configure) {
        if (permissions == null
                || permissions.isTrivial()
                || permissions.getMode() == PermissionMode.BYPASS) {
            throw new IllegalArgumentException(
                    "An explicit non-bypass permission policy is required");
        }
        for (String name : toolkit.getToolNames()) {
            if (!(toolkit.getTool(name) instanceof ToolBase)) {
                throw new IllegalArgumentException(
                        "Tool must extend ToolBase for permission evaluation: " + name);
            }
        }
        var builder =
                ReActAgent.builder()
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
                        .toolExecutionConfig(
                                io.agentscope.core.model.ExecutionConfig.builder()
                                        .maxAttempts(1)
                                        .build())
                        .maxIters(6)
                        .middleware(new FinalAnswerFilterMiddleware());
        if (fallback != null) {
            builder.fallbackModel(fallback);
        }
        configure.accept(builder);
        return builder.build();
    }

    public static RuntimeContext context(ExecutionScope scope) {
        return RuntimeContext.builder().userId(scope.userId()).sessionId(scope.sessionId()).build();
    }
}
