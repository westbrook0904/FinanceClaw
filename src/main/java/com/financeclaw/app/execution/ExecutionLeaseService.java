package com.financeclaw.app.execution;

import com.financeclaw.app.execution.port.*;
import com.financeclaw.app.task.TaskTransitionService;
import com.financeclaw.app.task.port.*;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;
import java.util.*;
import java.util.function.Supplier;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionTemplate;

/** 执行租约与 fencing 边界：命令锁决定当前执行尝试，短事务中的状态写入必须验证租约，过期执行不能续活或提交结果。 */
@Service
public class ExecutionLeaseService {
    private final CommandGateway db;
    private final TaskMutationGateway turns;
    private final TaskTransitionService transitions;
    private final TransactionTemplate tx;
    private final HeartbeatGateway heartbeats;
    private final CommandDispatch dispatch;

    /**
     * 显式构造的轻量 fixture 可复用其已有事务与命令端口。Spring 生产装配显式注入独立资源的 HeartbeatGateway 及可选调度端口；此入口不承担运行时自动降级。
     */
    public ExecutionLeaseService(
            CommandGateway db,
            TaskMutationGateway turns,
            TaskTransitionService transitions,
            TransactionTemplate tx) {
        this(
                db,
                turns,
                transitions,
                tx,
                (claim, lease) ->
                        tx.execute(
                                status -> {
                                    Boolean cancelling = db.renew(claim, lease);
                                    if (cancelling == null) throw new Fenced();
                                    return cancelling;
                                }));
    }

    public ExecutionLeaseService(
            CommandGateway db,
            TaskMutationGateway turns,
            TaskTransitionService transitions,
            TransactionTemplate tx,
            HeartbeatGateway heartbeats) {
        this(db, turns, transitions, tx, heartbeats, null);
    }

    public ExecutionLeaseService(
            CommandGateway db,
            TaskMutationGateway turns,
            TaskTransitionService transitions,
            TransactionTemplate tx,
            HeartbeatGateway heartbeats,
            CommandDispatch dispatch) {
        this.db = db;
        this.turns = turns;
        this.transitions = transitions;
        this.tx = tx;
        this.heartbeats = Objects.requireNonNull(heartbeats);
        this.dispatch = dispatch;
    }

    private static String id() {
        return UUID.randomUUID().toString();
    }

    public Claim claim(String worker, long leaseMillis) {
        return tx.execute(
                s -> {
                    Command c = dispatch == null ? db.nextCommand() : dispatch.next(worker);
                    if (c == null) {
                        return null;
                    }
                    Turn t = turns.lockTurn(c.turnId());
                    t.requireClaimable();
                    // command 的原始行锁仍保持到事务结束。使用 UPDATE RETURNING 的真实行，
                    // 只移除更新后的重复读取，不改变 command → Turn 锁序或后续独立执行栅栏。
                    c = db.claim(c.id(), id(), worker, leaseMillis);
                    if (c == null) {
                        // 零行不是一次成功领取；在事务回调内拒绝，避免继续写入无有效命令的 attempt。
                        throw new Fenced();
                    }
                    db.insertAttempt(c);
                    // 状态更新返回数据库真实 Turn；事件等副作用成功后再交还 Claim。
                    Turn running = transitions.changeAndReturn(t, "running", null, null);
                    if (dispatch != null) dispatch.claimed(c, running, worker);
                    return new Claim(c, running);
                });
    }

    private void current(Claim claim) {
        claim.requireCurrent(db.lockCommand(claim.command().id()));
    }

    /**
     * 在短事务内先锁 command 并验证租约、attempt 与 fence，再执行本地数据库动作。
     * 嵌套调用加入已有事务，但每次仍按数据库锁后的时间复核租约；不能把上次校验缓存为持续执行资格。 回调不得发起模型或远程调用，以免网络等待占住连接和阻塞取消所需的行锁。
     */
    public <T> T fenced(Claim claim, Supplier<T> action) {
        return tx.execute(
                s -> {
                    current(claim);
                    return action.get();
                });
    }

    /** 续期当前执行租约并返回取消状态；数据库在取得锁后判断租约有效性，不能复活已经过期的执行。 */
    public boolean heartbeat(Claim claim, long leaseMillis) {
        if (leaseMillis <= 0) {
            throw new IllegalArgumentException("Invalid lease duration");
        }
        // 不先进入业务 tx：否则借连接本身仍会排在业务池后面，并可能同时占用两个池。
        // 独立端口负责完整短事务及 null → Fenced 的事务内回滚，异常交由 Worker 中断 attempt。
        // 提交应答丢失时不能据异常推断租约未更新；Worker 保守停用，后续提交仍以数据库 fence 为准。
        return heartbeats.renew(claim, leaseMillis);
    }
}
