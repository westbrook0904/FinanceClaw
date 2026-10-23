package com.financeclaw.app.execution;

import com.financeclaw.app.execution.port.*;
import com.financeclaw.app.task.TaskTransitionService;
import com.financeclaw.app.task.port.*;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;
import java.util.*;
import org.springframework.stereotype.Service;

/** 执行结果提交器：先将检查点关联的回执封存，再完成业务状态转换；中间故障由恢复器读取持久回执继续收尾。 */
@Service
public class ExecutionCompletionService {
    private final TaskMutationGateway db;
    private final CommandGateway commands;
    private final ExecutionReceiptGateway receipts;
    private final ExecutionLeaseService leases;
    private final ExecutionPolicyService policies;
    private final TaskTransitionService transitions;

    public ExecutionCompletionService(
            TaskMutationGateway db,
            CommandGateway commands,
            ExecutionReceiptGateway receipts,
            ExecutionLeaseService leases,
            ExecutionPolicyService policies,
            TaskTransitionService transitions) {
        this.db = db;
        this.commands = commands;
        this.receipts = receipts;
        this.leases = leases;
        this.policies = policies;
        this.transitions = transitions;
    }

    private static String id() {
        return UUID.randomUUID().toString();
    }

    public void block(Claim claim, String reason) {
        leases.fenced(
                claim,
                () -> {
                    var t = db.lockTurn(claim.turn().id());
                    transitions.change(t, "blocked", null, reason);
                    commands.finishCommand(claim.command().id());
                    return null;
                });
    }

    /** 封存与当前检查点精确匹配的执行结果；暂停交互与回执一起提交，使恢复器能够继续同一尝试的后续收尾。 */
    public void seal(Claim claim, Outcome outcome) {
        if (!Set.of("completed", "waiting", "failed").contains(outcome.status())) {
            throw new IllegalArgumentException("Invalid outcome");
        }
        leases.fenced(
                claim,
                () -> {
                    var policy = policies.checkPolicy(claim);
                    if (!receipts.matchesCheckpoint(claim, outcome.stateVersion())) {
                        throw new Fenced();
                    }
                    if (receipts.seal(claim.command().attemptId(), outcome) != 1) {
                        throw new Fenced();
                    }
                    if (outcome.status().equals("waiting")) {
                        for (var p : outcome.pending()) {
                            receipts.insertInteraction(
                                    new Interaction(
                                            id(),
                                            claim.turn().id(),
                                            claim.command().id(),
                                            claim.command().attemptId(),
                                            outcome.stateVersion(),
                                            policy.grantRevision(),
                                            claim.turn().releaseHash(),
                                            p.kind(),
                                            p.callId(),
                                            p.tool(),
                                            p.actionHash(),
                                            p.schema(),
                                            p.question(),
                                            1,
                                            "pending",
                                            true,
                                            null,
                                            null));
                        }
                    }
                    return null;
                });
    }

    public void finish(Claim claim) {
        leases.fenced(
                claim,
                () -> {
                    finishReceipt(
                            commands.lockCommand(claim.command().id()),
                            db.lockTurn(claim.turn().id()));
                    return null;
                });
    }

    void finishReceipt(Command c, Turn t) {
        // 恢复只认已封存回执，不根据模型输出、流结束或内存标志推断业务成功。
        Receipt r = receipts.receipt(c.attemptId());
        if (r == null || r.outcome() == null || r.fence() != c.fence()) {
            throw new IllegalStateException("Missing sealed execution receipt");
        }
        boolean matching = receipts.matchesCheckpoint(new Claim(c, t), r.stateVersion());
        if (!t.status().equals("cancelling")) {
            try {
                policies.checkPolicy(new Claim(c, t));
            } catch (Blocked blocked) {
                transitions.change(t, "blocked", null, blocked.getMessage());
                commands.finishCommand(c.id());
                return;
            }
        }
        if (!matching) {
            transitions.change(t, "blocked", null, "checkpoint_mismatch");
        } else if (t.status().equals("cancelling")) {
            transitions.change(t, "cancelled", null, "cancel_confirmed");
        } else {
            transitions.change(
                    t,
                    r.outcome(),
                    r.result(),
                    r.outcome().equals("failed") ? "execution_failed" : null);
        }
        commands.finishCommand(c.id());
    }

    /** 原生执行器及其资源停止后确认取消；此时无需保存无业务意义的运行时快照。 */
    public boolean acknowledgeCancel(Claim claim) {
        return leases.fenced(
                claim,
                () -> {
                    Turn t = db.lockTurn(claim.turn().id());
                    if (!t.status().equals("cancelling")) {
                        return false;
                    }
                    transitions.change(t, "cancelled", null, "cancel_confirmed");
                    commands.finishCommand(claim.command().id());
                    return true;
                });
    }

    public void fail(Claim claim) {
        leases.fenced(
                claim,
                () -> {
                    Turn t = db.lockTurn(claim.turn().id());
                    transitions.change(
                            t,
                            t.status().equals("cancelling") ? "cancelled" : "failed",
                            null,
                            t.status().equals("cancelling")
                                    ? "cancel_confirmed"
                                    : "execution_failed");
                    commands.finishCommand(claim.command().id());
                    return null;
                });
    }
}
