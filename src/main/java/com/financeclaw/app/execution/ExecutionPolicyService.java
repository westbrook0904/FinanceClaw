package com.financeclaw.app.execution;

import com.financeclaw.app.execution.port.*;
import com.financeclaw.app.task.TaskEventHub;
import com.financeclaw.app.task.port.*;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;
import com.financeclaw.support.PayloadJson;
import java.util.*;
import org.springframework.stereotype.Service;

/** 执行授权与预算边界：每次模型或工具调用前核验固定发布版本、用户隐私和权限版本，并在相同栅栏事务中扣减预算。 */
@Service
public class ExecutionPolicyService {
    private final TaskQueryGateway db;
    private final TaskEventHub events;
    private final ExecutionPolicyGateway governance;
    private final ExecutionRelease releases;
    private final ExecutionMemory memory;
    private final ExecutionLeaseService leases;

    public ExecutionPolicyService(
            TaskQueryGateway db,
            TaskEventHub events,
            ExecutionPolicyGateway governance,
            ExecutionRelease releases,
            ExecutionMemory memory,
            ExecutionLeaseService leases) {
        this.db = db;
        this.events = events;
        this.governance = governance;
        this.releases = releases;
        this.memory = memory;
        this.leases = leases;
    }

    public String memoryContext(Claim claim) {
        return memory.context(claim.turn());
    }

    public void checkDispatch(Claim claim) {
        leases.fenced(
                claim,
                () -> {
                    if (!db.turn(claim.turn().id()).status().equals("running")) {
                        throw new Fenced();
                    }
                    checkPolicy(claim);
                    return null;
                });
    }

    /** 调用方先锁命令，再按隐私用户、授权行的顺序加锁，使执行写入与撤权、遗忘互斥。 */
    public Policy checkPolicy(Claim claim) {
        releases.require(claim.turn().releaseHash());
        memory.check(claim.turn());
        var p = governance.policy(claim.turn().id());
        var grant = governance.lockGrant(claim.turn().conversationId());
        if (p == null || grant == null || p.grantRevision() != grant.revision()) {
            throw new Blocked("authorization_changed");
        }
        return p;
    }

    public List<Turn> history(Claim claim) {
        return leases.fenced(
                claim,
                () -> {
                    var p = checkPolicy(claim);
                    return memory.filterHistory(
                            claim.turn(),
                            db.history(
                                    claim.turn().conversationId(),
                                    claim.turn().releaseHash(),
                                    p.grantRevision()));
                });
    }

    public ExecutionIdentity identity(Claim claim) {
        var p = governance.policy(claim.turn().id());
        var t = claim.turn();
        return new ExecutionIdentity(
                t.tenant(),
                t.subject(),
                t.conversationId(),
                t.id(),
                claim.command().attemptId(),
                t.releaseHash(),
                p.grantRevision(),
                List.copyOf(PayloadJson.scopes(p.scopes())),
                governance.startedAt(claim.command().attemptId()));
    }

    public void checkArtifactPrivacy(Turn turn) {
        if (!memory.sameEpoch(turn)) {
            throw new Problem(409, "privacy_epoch_changed");
        }
    }

    public void consume(Claim claim, boolean model) {
        consume(claim, model, null);
    }

    /** 工具范围校验与预算扣减共享同一租约及授权行锁，避免撤权发生在校验与扣减之间。 */
    public void consumeTool(Claim claim, String scope) {
        consume(claim, false, scope);
    }

    private void consume(Claim claim, boolean model, String scope) {
        leases.fenced(
                claim,
                () -> {
                    if (!db.turn(claim.turn().id()).status().equals("running")) {
                        throw new Fenced();
                    }
                    var p = checkPolicy(claim);
                    if (scope != null && !PayloadJson.scopes(p.scopes()).contains(scope)) {
                        throw new Blocked("tool_denied");
                    }
                    var r = releases.require(claim.turn().releaseHash());
                    int changed =
                            model
                                    ? governance.modelCall(claim.turn().id(), r.maxModelCalls())
                                    : governance.toolCall(claim.turn().id(), r.maxToolCalls());
                    if (changed != 1) {
                        throw new Blocked("execution_budget_exhausted");
                    }
                    return null;
                });
    }

    public TaskEventHub.Progress beginProgress(Claim claim) {
        return events.begin(claim);
    }

    public void progress(Claim claim, String kind, String text, String tool) {
        try {
            events.progress(claim, kind, text, tool);
        } catch (Fenced invalidated) {
            // Only rejected progress needs SQL: preserve an authoritative revocation/privacy
            // reason.
            checkDispatch(claim);
            throw invalidated;
        }
    }

    public List<String> scopes(Claim claim) {
        return List.copyOf(PayloadJson.scopes(governance.policy(claim.turn().id()).scopes()));
    }
}
