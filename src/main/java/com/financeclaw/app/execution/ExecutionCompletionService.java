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

    /**
     * 封存与检查点精确匹配的结果，并将暂停交互与回执一起提交；此时尚未宣布 Turn 完成。 正常执行先提交回执再调用 finish；进程在两步之间退出时，恢复器仍可收尾同一
     * attempt，无需重跑模型或工具。
     */
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
                        // 未命中有效 attempt 时在同一事务内拒绝，不能留下没有封存回执的暂停交互。
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

    /** 正常执行在封存后另起收尾短事务，重新检查当前授权、checkpoint 和取消状态，再提交业务终态。 */
    public void finish(Claim claim) {
        leases.fenced(
                claim,
                () -> {
                    // 保留原 command → Turn 锁序；读取发生在同一执行栅栏事务内。
                    Command c = commands.lockCommand(claim.command().id());
                    Turn t = db.lockTurn(claim.turn().id());
                    Receipt r = receipts.receipt(c.attemptId());
                    finishReceipt(c, t, r);
                    return null;
                });
    }

    /** 使用调用方在同一事务、command → Turn 持锁范围内读取的封存回执继续收尾。 本方法不建立事务或重新查询回执，参数只能在本次调用栈中传递，不能跨事务缓存。 */
    void finishReceipt(Command c, Turn t, Receipt r) {
        // 恢复器持有过期 command 的锁，因此这里不再要求活租约；也不创建新 attempt。
        // 正常完成入口已经过 leases.fenced，恢复入口则只凭当前持锁行与封存回执继续收尾。
        // 恢复只认已封存回执，不根据模型输出、流结束或内存标志推断业务成功。
        // 原先按 attempt 主键查询隐含的 id 绑定必须显式保留；同时核验 commandId，
        // 防止数据库错误关联或错误调用方把其他命令的同 fence 回执用于当前执行。
        if (r == null
                || r.outcome() == null
                || !Objects.equals(r.id(), c.attemptId())
                || !Objects.equals(r.commandId(), c.id())
                || r.fence() != c.fence()) {
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
