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
     * Worker 从事务外调用本方法，每项短事务完成后才领取下一项，每轮最多 50 项，空候选即停止。 SKIP LOCKED 只跳过其他实例持有的 command
     * 锁，Turn、策略锁或连接池仍可能等待。
     *
     * <p>回调内失败会请求回滚；提交阶段异常则可能已提交，不能一律解释为回滚。本轮停止并传播异常， 保留前项已提交结果；下一轮重新读取持久 command 和回执，不重放结果未知的外部执行。
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
