package com.financeclaw.app.task;

import com.financeclaw.app.execution.port.*;
import com.financeclaw.app.task.port.*;
import com.financeclaw.domain.Task.*;
import com.financeclaw.domain.TaskTransition;
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

    /** 加入调用方短事务；普通迁移只写入，不额外把问题、结果等 Turn 正文返回应用层。 */
    public void change(Turn t, String status, String result, String reason) {
        var transition = prepare(t, status, result, reason);
        db.transition(t.id(), status, result, reason);
        complete(transition);
    }

    /**
     * 领取需要更新后的真实 Turn：由同一 UPDATE RETURNING 返回，避免另一次 SELECT。
     * 仅领取链路使用该入口；状态事件与完成副作用仍全部执行成功后，才向调用方返回数据库对象。
     */
    public Turn changeAndReturn(Turn t, String status, String result, String reason) {
        var transition = prepare(t, status, result, reason);
        Turn updated = db.transitionAndReturn(t.id(), status, result, reason);
        if (updated == null) {
            // 不合成旧 Turn，也不回退到 SELECT；外层事务回滚此前的交互失效和领取写入。
            // 尚未写事件，因此不会注册不存在的状态变化通知。
            throw new Fenced();
        }
        complete(transition);
        return updated;
    }

    /** 两入口共有的写入前置条件，顺序保持为事务、领域规则、交互失效。 */
    private TaskTransition prepare(Turn t, String status, String result, String reason) {
        if (!org.springframework.transaction.support.TransactionSynchronizationManager
                .isActualTransactionActive()) {
            throw new IllegalStateException("Task transition requires a transaction");
        }
        var transition = new TaskTransition(t, status, result, reason);
        if (transition.invalidatesInteractions()) {
            governance.invalidate(t.id());
        }
        return transition;
    }

    /** 保留原迁移参数的副作用含义与先后：事件、最终会话记录、记忆任务同事务提交。 RETURNING 行只用于调用方获取真实更新结果，不替代原输入重新决定 journal 或记忆行为。 */
    private void complete(TaskTransition transition) {
        Turn t = transition.turn();
        db.event(t.id());
        if (transition.completed()) {
            db.journal(t.conversationId(), t.id(), "assistant", transition.result());
            memory.completed(t.id());
        }
    }
}
