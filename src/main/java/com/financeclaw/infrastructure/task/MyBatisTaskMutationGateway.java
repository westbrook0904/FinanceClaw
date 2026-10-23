package com.financeclaw.infrastructure.task;

import com.financeclaw.app.task.TaskEventHub;
import com.financeclaw.app.task.port.TaskMutationGateway;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;
import org.springframework.stereotype.Repository;

/**
 * 实现 TaskMutationGateway，保存 Turn 迁移、取消回执、journal 和持久业务事件。
 * 状态修改加入调用方已加锁的事务，事件中心在提交后通知订阅者，回滚不会对外发布成功迁移。该适配器不决定业务状态机是否合法。
 */
@Repository
public class MyBatisTaskMutationGateway implements TaskMutationGateway {
    private final TaskMapper db;
    private final TaskEventHub events;

    public MyBatisTaskMutationGateway(TaskMapper db, TaskEventHub events) {
        this.events = events;
        this.db = db;
    }

    @Override
    public Turn lockTurn(String id) {
        return db.getTurnForUpdate(id);
    }

    @Override
    public Accepted cancelReplay(String turn, String key) {
        return db.getCancelReceiptByIdempotencyKey(turn, key);
    }

    @Override
    public void recordCancel(String turn, String key, String status, long revision) {
        db.insertCancelReceipt(turn, key, status, revision);
    }

    @Override
    public void transition(String turn, String status, String result, String reason) {
        db.updateTurnState(turn, status, result, reason);
    }

    @Override
    public void event(String turn) {
        events.committed(db.insertTurnEventAndReturn(turn));
    }

    @Override
    public void journal(String conversation, String turn, String role, String content) {
        db.insertJournalEntryIfAbsent(conversation, turn, role, content);
    }
}
