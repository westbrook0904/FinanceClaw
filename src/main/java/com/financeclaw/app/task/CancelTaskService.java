package com.financeclaw.app.task;

import com.financeclaw.app.execution.port.*;
import com.financeclaw.app.task.port.*;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.IdempotencyKey;
import com.financeclaw.domain.Task.*;
import java.util.*;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionTemplate;

/** 任务取消入口：运行中的执行先进入 cancelling，只有执行器确认停止才进入 cancelled；未执行的命令可以直接关闭。取消回执与状态变更原子提交。 */
@Service
public class CancelTaskService {
    private final TaskQueryService queries;
    private final TaskQueryGateway reads;
    private final TaskMutationGateway db;
    private final CommandGateway commands;
    private final TaskTransitionService transitions;
    private final TransactionTemplate tx;

    public CancelTaskService(
            TaskQueryService queries,
            TaskQueryGateway reads,
            TaskMutationGateway db,
            CommandGateway commands,
            TaskTransitionService transitions,
            TransactionTemplate tx) {
        this.queries = queries;
        this.reads = reads;
        this.db = db;
        this.commands = commands;
        this.transitions = transitions;
        this.tx = tx;
    }

    /** 按命令锁先于任务锁的顺序取消；运行中的执行先确认停止，再写入终态。重复请求返回原始取消回执。 */
    public Accepted cancel(Owner owner, String conversation, String turn, String key) {
        new IdempotencyKey(key);
        queries.get(owner, conversation, turn);
        return tx.execute(
                s -> {
                    Command c = commands.lockTurnCommand(turn);
                    Turn t = db.lockTurn(turn);
                    Accepted replay = db.cancelReplay(turn, key);
                    if (replay != null) {
                        return replay;
                    }
                    if (!t.terminal() && !t.status().equals("cancelling")) {
                        if (c.status().equals("running")) {
                            transitions.change(t, "cancelling", null, "cancel_requested");
                        } else {
                            transitions.change(t, "cancelled", null, "cancel_requested");
                            commands.finishCommand(c.id());
                        }
                    }
                    t = reads.turn(turn);
                    db.recordCancel(turn, key, t.status(), t.revision());
                    return t.accepted(false);
                });
    }
}
