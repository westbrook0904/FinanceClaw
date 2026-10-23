package com.financeclaw.infrastructure.task;

import com.financeclaw.app.execution.port.ExecutionReceiptGateway;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;
import com.financeclaw.infrastructure.governance.GovernanceMapper;
import com.financeclaw.infrastructure.state.RuntimeMapper;
import org.springframework.stereotype.Repository;

/**
 * 实现 ExecutionReceiptGateway，对齐 AgentScope 检查点与业务 attempt 的封存回执。 检查点必须存在、未删除且版本匹配，首次封存由 SQL
 * 条件保证；人工交互与回执写入加入完成用例持有的执行栅栏事务。保存 SDK 状态本身不代表业务已完成。
 */
@Repository
public class MyBatisExecutionReceiptGateway implements ExecutionReceiptGateway {
    private final TaskMapper db;
    private final RuntimeMapper states;
    private final GovernanceMapper governance;

    public MyBatisExecutionReceiptGateway(
            TaskMapper db, RuntimeMapper states, GovernanceMapper governance) {
        this.db = db;
        this.states = states;
        this.governance = governance;
    }

    @Override
    public boolean matchesCheckpoint(Claim claim, long version) {
        var scope = claim.scope();
        var row =
                states.getState(
                        new RuntimeMapper.StateKey(
                                scope.namespace(),
                                scope.userId(),
                                scope.sessionId(),
                                "agent_state"));
        return row != null && !row.deleted() && row.version() == version;
    }

    @Override
    public int seal(String attempt, Outcome outcome) {
        return db.updateAttemptOutcomeIfUnsealed(attempt, outcome);
    }

    @Override
    public Receipt receipt(String attempt) {
        return db.getAttemptReceipt(attempt);
    }

    @Override
    public void insertInteraction(Interaction interaction) {
        governance.insertInteraction(interaction);
    }
}
