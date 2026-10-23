package com.financeclaw.infrastructure.task;

import com.financeclaw.app.execution.port.ExecutionPolicyGateway;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;
import com.financeclaw.infrastructure.governance.GovernanceMapper;
import org.springframework.stereotype.Repository;

/**
 * 实现 ExecutionPolicyGateway，读写当前 grant、Turn 授权快照和模型工具计数。 授权锁、快照版本核验及预算扣减由 ExecutionPolicyService
 * 在同一执行栅栏事务中组合。SQL 原子返回影响行数，用例据此识别预算不足，不通过先查后加预留额度。
 */
@Repository
public class MyBatisExecutionPolicyGateway implements ExecutionPolicyGateway {
    private final GovernanceMapper db;

    public MyBatisExecutionPolicyGateway(GovernanceMapper db) {
        this.db = db;
    }

    @Override
    public void ensureGrant(String conversation, String scopes) {
        db.insertGrantIfAbsent(conversation, scopes);
    }

    @Override
    public Grant lockGrant(String conversation) {
        return db.getGrantForUpdate(conversation);
    }

    @Override
    public void insertPolicy(String turn, long revision, String scopes) {
        db.insertTurnPolicy(turn, revision, scopes);
    }

    @Override
    public Policy policy(String turn) {
        return db.getTurnPolicy(turn);
    }

    @Override
    public int modelCall(String turn, int max) {
        return db.updateModelCallCountWithinLimit(turn, max);
    }

    @Override
    public int toolCall(String turn, int max) {
        return db.updateToolCallCountWithinLimit(turn, max);
    }

    @Override
    public void invalidate(String turn) {
        db.updatePendingInteractionsInvalid(turn);
    }

    @Override
    public String startedAt(String attempt) {
        return db.getAttemptStartedAt(attempt);
    }
}
