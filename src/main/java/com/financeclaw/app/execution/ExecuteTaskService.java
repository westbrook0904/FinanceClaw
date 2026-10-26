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
            completion.seal(claim, result);
            completion.finish(claim);
        } catch (com.financeclaw.domain.Governance.Blocked blocked) {
            try {
                completion.block(claim, blocked.getMessage());
            } catch (RuntimeException lost) {
                /* recover expired lease */
            }
        } catch (RuntimeException ignored) {
            /* Preserve a sealed receipt; never turn commit uncertainty into failure. */
        }
    }

    public void interrupt(Claim claim) {
        runtime.interrupt(claim);
    }
}
