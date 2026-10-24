package com.financeclaw.infrastructure.task;

import com.financeclaw.app.execution.port.ChildInvocationGateway;
import com.financeclaw.domain.execution.ChildInvocation;
import com.financeclaw.infrastructure.governance.GovernanceMapper;
import com.financeclaw.support.PayloadJson;
import java.util.List;
import org.springframework.stereotype.Repository;

/** 复用治理 Mapper，读取根身份及现有步骤回执，避免复制子调用结果和恢复权威。 */
@Repository
public class MyBatisChildInvocationGateway implements ChildInvocationGateway {
    private final GovernanceMapper db;

    public MyBatisChildInvocationGateway(GovernanceMapper db) {
        this.db = db;
    }

    @Override
    public ChildInvocation get(String root, String call) {
        var row = db.getChildInvocation(root, call);
        return row == null
                ? null
                : new ChildInvocation(
                        row.rootTurnId(),
                        row.callId(),
                        row.target(),
                        row.targetAgentId(),
                        row.targetReleaseHash(),
                        row.inputHash(),
                        PayloadJson.scopes(row.scopes()),
                        row.status(),
                        row.modelCalls(),
                        row.toolCalls(),
                        row.tenant(),
                        row.subject(),
                        row.conversationId(),
                        row.releaseHash(),
                        row.epoch(),
                        row.grantRevision(),
                        row.resultHash(),
                        row.result());
    }

    @Override
    public int insertIfAbsent(String root, String call, String hash) {
        return db.insertChildInvocationIfAbsent(root, call, hash);
    }

    @Override
    public int insertPrivateIfAbsent(
            String root, String call, String hash, String targetAgent, String targetRelease) {
        return db.insertPrivateChildInvocationIfAbsent(
                root, call, hash, targetAgent, targetRelease);
    }

    @Override
    public int insertPrivateMarketIfAbsent(
            String root, String call, String hash, String targetAgent, String targetRelease) {
        return db.insertPrivateMarketChildInvocationIfAbsent(
                root, call, hash, targetAgent, targetRelease);
    }

    @Override
    public int reserveModel(String root, String call, String hash, int limit) {
        return db.updatePendingChildModelCountWithinLimit(root, call, hash, limit);
    }

    @Override
    public int reserveTool(String root, String call, String hash) {
        return db.updatePendingChildToolCountWithinLimit(root, call, hash);
    }

    @Override
    public int reservePrivateMarketTool(String root, String call, String hash) {
        return db.updatePendingPrivateMarketToolCountWithinLimit(root, call, hash);
    }

    @Override
    public int complete(String root, String call, String hash) {
        return db.updatePendingChildInvocationCompleted(root, call, hash);
    }

    @Override
    public boolean hasSteps(String root, String namespace) {
        return db.existsCapabilitySteps(root, namespace);
    }

    @Override
    public List<String> snapshots(String root, String namespace) {
        return db.listChildSnapshotResults(root, namespace);
    }
}
