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

    /**
     * 每轮最多开启 50 个领取事务，空候选即停止；每项成功提交后才领取下一项。 SKIP LOCKED 只避免 command 行上的实例竞争，不承诺 Turn、策略锁或连接池绝不等待。
     * 异常继续向上抛出：当前项回滚，已提交的前项保留；不在本方法重试结果未知的外部效果。
     */
    public int recoverExpired() {
        int count = 0;
        for (int inspected = 0; inspected < 50; inspected++) {
            RecoveryStep step =
                    Objects.requireNonNull(
                            tx.execute(
                                    s -> {
                                        Command c = db.nextExpiredCommand();
                                        if (c == null) return RecoveryStep.EMPTY;
                                        if (c.live() || !c.status().equals("running")) {
                                            return RecoveryStep.UNCHANGED;
                                        }
                                        Turn t = turns.lockTurn(c.turnId());
                                        Receipt r = receipts.receipt(c.attemptId());
                                        if (r != null && r.outcome() != null) {
                                            // Command 与 Turn 始终由本次事务持锁；沿用已读回执、
                                            // checkpoint、实时 policy、取消优先级和身份/fence 校验。
                                            completion.finishReceipt(c, t, r);
                                        } else {
                                            transitions.change(
                                                    t, "blocked", null, "unconfirmed_execution");
                                            db.finishCommand(c.id());
                                        }
                                        return RecoveryStep.RECOVERED;
                                    }),
                            "Recovery transaction did not return a step");
            if (step == RecoveryStep.EMPTY) break;
            if (step == RecoveryStep.RECOVERED) count++;
        }
        return count;
    }

    private enum RecoveryStep {
        EMPTY,
        UNCHANGED,
        RECOVERED
    }
}
