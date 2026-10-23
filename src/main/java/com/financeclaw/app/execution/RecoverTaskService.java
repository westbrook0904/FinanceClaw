package com.financeclaw.app.execution;

import com.financeclaw.app.execution.port.*;
import com.financeclaw.app.task.TaskTransitionService;
import com.financeclaw.app.task.port.*;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;
import java.util.*;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionTemplate;

/** 过期命令恢复器：存在已封存回执时继续收尾；缺少确定回执时进入人工核对状态，避免自动重放结果未知的执行。 */
@Service
public class RecoverTaskService {
    private final CommandGateway db;
    private final TaskMutationGateway turns;
    private final ExecutionReceiptGateway receipts;
    private final ExecutionCompletionService completion;
    private final TaskTransitionService transitions;
    private final TransactionTemplate tx;

    public RecoverTaskService(
            CommandGateway db,
            TaskMutationGateway turns,
            ExecutionReceiptGateway receipts,
            ExecutionCompletionService completion,
            TaskTransitionService transitions,
            TransactionTemplate tx) {
        this.db = db;
        this.turns = turns;
        this.receipts = receipts;
        this.completion = completion;
        this.transitions = transitions;
        this.tx = tx;
    }

    /** 逐个使用独立短事务核验过期命令。有已封存回执时收尾；结果未知时阻断，不能用重新执行猜测结果。 */
    public int recoverExpired() {
        int count = 0;
        for (String id : db.expired()) {
            boolean changed =
                    Boolean.TRUE.equals(
                            tx.execute(
                                    s -> {
                                        Command c = db.lockCommand(id);
                                        if (c == null
                                                || c.live()
                                                || !c.status().equals("running")) {
                                            return false;
                                        }
                                        Turn t = turns.lockTurn(c.turnId());
                                        Receipt r = receipts.receipt(c.attemptId());
                                        if (r != null && r.outcome() != null) {
                                            completion.finishReceipt(c, t);
                                        } else {
                                            transitions.change(
                                                    t, "blocked", null, "unconfirmed_execution");
                                            db.finishCommand(id);
                                        }
                                        return true;
                                    }));
            if (changed) {
                count++;
            }
        }
        return count;
    }
}
