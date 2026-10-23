package com.financeclaw.app.execution;

import com.financeclaw.app.execution.port.*;
import com.financeclaw.app.task.TaskEventHub;
import com.financeclaw.app.task.port.*;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;
import com.financeclaw.support.PayloadJson;
import java.util.*;
import java.util.function.Consumer;
import java.util.function.Function;
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
        dispatch(claim, ignored -> null);
    }

    /** 在一次父执行栅栏事务内完成运行状态、隐私和授权核验，再执行本地工作。 子调用只能借用父 Claim，不能形成另一个调度或授权权威；回调禁止发起模型、网络等远程调用。 */
    public <T> T dispatch(Claim claim, Function<Policy, T> localWork) {
        return leases.fenced(
                claim,
                () -> {
                    if (!db.turn(claim.turn().id()).status().equals("running")) {
                        throw new Fenced();
                    }
                    return localWork.apply(checkPolicy(claim));
                });
    }

    /** 固定发布与受理授权的交集；只能在已通过 dispatch 的短事务内使用。 */
    public List<String> effectiveScopes(Claim claim, Policy policy) {
        var allowed = releases.requireForTurn(claim.turn()).scopes();
        return PayloadJson.scopes(policy.scopes()).stream().filter(allowed::contains).toList();
    }

    /** 调用方先锁命令，再按隐私用户、授权行的顺序加锁，使执行写入与撤权、遗忘互斥。 */
    public Policy checkPolicy(Claim claim) {
        releases.requireForTurn(claim.turn());
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
        consume(claim, model, null, ignored -> {});
    }

    /** 子额度和根额度同一事务提交；任一预留失败，另一方的计数也回滚。模型请求在该方法返回后发起。 */
    public void consumeModel(Claim claim, Consumer<Policy> reserveChild) {
        consume(claim, true, null, reserveChild);
    }

    /** 工具范围校验与预算扣减共享同一租约及授权行锁，避免撤权发生在校验与扣减之间。 */
    public void consumeTool(Claim claim, String scope) {
        consume(claim, false, scope, ignored -> {});
    }

    private void consume(Claim claim, boolean model, String scope, Consumer<Policy> reserveChild) {
        dispatch(
                claim,
                p -> {
                    if (scope != null && !PayloadJson.scopes(p.scopes()).contains(scope)) {
                        throw new Blocked("tool_denied");
                    }
                    reserveChild.accept(p);
                    var r = releases.requireForTurn(claim.turn());
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
