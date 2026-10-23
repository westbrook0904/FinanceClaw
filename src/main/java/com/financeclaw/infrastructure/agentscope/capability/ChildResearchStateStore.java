package com.financeclaw.infrastructure.agentscope.capability;

import com.financeclaw.app.research.MarketResearchService;
import com.financeclaw.domain.ExecutionScope;
import com.financeclaw.infrastructure.state.MyBatisAgentStateStore;
import com.financeclaw.infrastructure.state.RuntimeMapper;
import io.agentscope.core.state.*;
import java.util.List;

/**
 * 子研究检查点的受治理适配器。读取与写入均借用同一父 Claim，不能仅凭 SDK 的 user/session 哈希访问旧状态；父取消、隐私 epoch
 * 改变或授权撤销后，迟到回调必须先被用例门禁拒绝。
 */
public final class ChildResearchStateStore extends MyBatisAgentStateStore {
    private final MarketResearchService research;
    private final MarketResearchService.Execution execution;

    public ChildResearchStateStore(
            RuntimeMapper mapper,
            ExecutionScope scope,
            MarketResearchService research,
            MarketResearchService.Execution execution) {
        super(mapper, scope);
        if (execution.run() == null || !scope.equals(execution.run().scope())) {
            throw new IllegalArgumentException("Child checkpoint scope does not match invocation");
        }
        this.research = research;
        this.execution = execution;
    }

    @Override
    public <T extends State> VersionedState<T> getVersioned(
            String user, String session, String key, Class<T> type) {
        return research.checkpoint(execution, () -> super.getVersioned(user, session, key, type));
    }

    @Override
    public long saveIfVersion(String user, String session, String key, State state, long expected) {
        return research.checkpoint(
                execution, () -> super.saveIfVersion(user, session, key, state, expected));
    }

    @Override
    public <T extends State> List<T> getList(
            String user, String session, String key, Class<T> type) {
        return research.checkpoint(execution, () -> super.getList(user, session, key, type));
    }

    @Override
    public boolean exists(String user, String session) {
        return research.checkpoint(execution, () -> super.exists(user, session));
    }

    @Override
    public void delete(String user, String session, String key) {
        research.checkpoint(
                execution,
                () -> {
                    super.delete(user, session, key);
                    return null;
                });
    }
}
