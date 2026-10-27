package com.financeclaw.app.execution;

import com.financeclaw.app.execution.port.*;
import com.financeclaw.app.task.port.*;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;
import java.util.*;
import org.springframework.stereotype.Service;

/** 单次执行编排器：调用 Agent 运行时并处理完成、暂停、取消和失败；只有业务回执完成提交才算任务完成。 */
@Service
public class ExecuteTaskService {
    private final ExecutionCompletionService completion;
    private final AgentExecutionPort runtime;

    public ExecuteTaskService(ExecutionCompletionService completion, AgentExecutionPort runtime) {
        this.completion = completion;
        this.runtime = runtime;
    }

    public void execute(Claim claim) {
        executeUsing(claim, () -> runtime.execute(claim));
    }

    public void execute(Claim claim, java.util.function.BooleanSupplier allowed) {
        executeUsing(claim, () -> runtime.execute(claim, allowed));
    }

    private void executeUsing(Claim claim, java.util.function.Supplier<Outcome> invocation) {
        // SDK/网络调用跨越多个短栅栏事务；外层事务会一直占住连接和行锁，妨碍续租、取消与撤权。
        if (org.springframework.transaction.support.TransactionSynchronizationManager
                .isActualTransactionActive()) {
            throw new IllegalStateException("Native execution must run outside a transaction");
        }
        Outcome result;
        try {
            result = invocation.get();
        } catch (com.financeclaw.domain.Governance.Blocked blocked) {
            try {
                completion.block(claim, blocked.getMessage());
            } catch (RuntimeException lost) {
                /* lease recovery */
            }
            return;
        } catch (Fenced ignored) {
            // Cancellation may win after claim but before SDK dispatch. Confirm that stopped
            // attempt.
            try {
                completion.fail(claim);
            } catch (RuntimeException lost) {
                /* Expired owner remains fenced. */
            }
            return;
        } catch (RuntimeException error) {
            try {
                completion.fail(claim);
            } catch (RuntimeException ignored) {
                /* Reconcile after lease expiry. */
            }
            return;
        }
        try {
            if (completion.acknowledgeCancel(claim)) {
                return;
            }
            // 两个独立短事务保留“已封存、尚未收尾”的恢复点；finish 失败仍可凭回执完成同一 attempt。
            completion.seal(claim, result);
            completion.finish(claim);
        } catch (com.financeclaw.domain.Governance.Blocked blocked) {
            try {
                completion.block(claim, blocked.getMessage());
            } catch (RuntimeException lost) {
                /* recover expired lease */
            }
        } catch (RuntimeException ignored) {
            // 提交应答丢失或 afterCommit 回调异常都不能证明数据库回滚。
            // 保留可能已封存的回执，交给租约恢复核验；此处补写 failed 会覆盖尚待确认的成功结果。
        }
    }

    public void interrupt(Claim claim) {
        runtime.interrupt(claim);
    }
}
