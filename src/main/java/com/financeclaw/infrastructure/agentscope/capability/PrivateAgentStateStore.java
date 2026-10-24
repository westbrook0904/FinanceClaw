package com.financeclaw.infrastructure.agentscope.capability;

import com.financeclaw.app.execution.ChildInvocationService;
import com.financeclaw.app.execution.ChildInvocationService.Invocation;
import com.financeclaw.domain.ExecutionScope;
import com.financeclaw.domain.agent.PrivateAgentTarget;
import com.financeclaw.infrastructure.state.MyBatisAgentStateStore;
import com.financeclaw.infrastructure.state.RuntimeMapper;
import io.agentscope.core.state.*;
import java.util.List;

/** 叶子 Agent 的检查点仍受父 Claim 栅栏保护，SDK 哈希本身不赋予读取或写入权限。 */
public final class PrivateAgentStateStore extends MyBatisAgentStateStore {
    private final ChildInvocationService calls;
    private final Invocation invocation;

    public PrivateAgentStateStore(
            RuntimeMapper mapper,
            ExecutionScope scope,
            ChildInvocationService calls,
            Invocation invocation) {
        super(mapper, scope);
        if (!"private_agent".equals(invocation.target())
                || !scope.equals(
                        new PrivateAgentTarget(
                                        invocation.targetAgentId(), invocation.targetReleaseHash())
                                .scope(invocation.parent().scope(), invocation.callId()))) {
            throw new IllegalArgumentException("Child checkpoint scope does not match invocation");
        }
        this.calls = calls;
        this.invocation = invocation;
    }

    @Override
    public <T extends State> VersionedState<T> getVersioned(
            String user, String session, String key, Class<T> type) {
        return calls.checkpoint(invocation, () -> super.getVersioned(user, session, key, type));
    }

    @Override
    public long saveIfVersion(String user, String session, String key, State state, long expected) {
        return calls.checkpoint(
                invocation, () -> super.saveIfVersion(user, session, key, state, expected));
    }

    @Override
    public <T extends State> List<T> getList(
            String user, String session, String key, Class<T> type) {
        return calls.checkpoint(invocation, () -> super.getList(user, session, key, type));
    }

    @Override
    public boolean exists(String user, String session) {
        return calls.checkpoint(invocation, () -> super.exists(user, session));
    }

    @Override
    public void delete(String user, String session, String key) {
        calls.checkpoint(
                invocation,
                () -> {
                    super.delete(user, session, key);
                    return null;
                });
    }
}
