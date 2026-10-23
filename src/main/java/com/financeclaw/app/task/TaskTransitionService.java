package com.financeclaw.app.task;

import com.financeclaw.app.execution.port.*;
import com.financeclaw.app.task.port.*;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;
import java.util.*;
import org.springframework.stereotype.Service;

/** 统一提交任务状态、可回放事件及最终会话记录。调用方负责事务与执行栅栏，外部通知由提交后的事件机制驱动。 */
@Service
public class TaskTransitionService {
    private final TaskMutationGateway db;
    private final ExecutionPolicyGateway governance;
    private final ExecutionMemory memory;

    public TaskTransitionService(
            TaskMutationGateway db, ExecutionPolicyGateway governance, ExecutionMemory memory) {
        this.db = db;
        this.governance = governance;
        this.memory = memory;
    }

    /** 加入调用方短事务，任务状态、事件、最终会话记录与证据提取任务共同提交。 */
    public void change(Turn t, String status, String result, String reason) {
        if (!org.springframework.transaction.support.TransactionSynchronizationManager
                .isActualTransactionActive()) {
            throw new IllegalStateException("Task transition requires a transaction");
        }
        var transition = new com.financeclaw.domain.TaskTransition(t, status, result, reason);
        if (transition.invalidatesInteractions()) {
            governance.invalidate(t.id());
        }
        db.transition(t.id(), status, result, reason);
        db.event(t.id());
        if (transition.completed()) {
            db.journal(t.conversationId(), t.id(), "assistant", result);
            memory.completed(t.id());
        }
    }
}
