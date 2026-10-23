package com.financeclaw.infrastructure.task;

import com.financeclaw.app.task.TaskEventHub;
import com.financeclaw.app.task.WorkSignals;
import com.financeclaw.app.task.port.TaskAdmissionGateway;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;
import org.springframework.stereotype.Repository;

/**
 * 实现 TaskAdmissionGateway，持久化会话、Turn 和受理时的调度记录。 命令、用户 journal
 * 与业务事件加入同一个受理事务；提交后才通过事件中心和工作信号唤醒本地消费者。owner 校验及受理共享锁由对应 SQL 配合用例完成。
 */
@Repository
public class MyBatisTaskAdmissionGateway implements TaskAdmissionGateway {
    private final TaskMapper db;
    private final TaskEventHub events;
    private final WorkSignals signals;

    public MyBatisTaskAdmissionGateway(TaskMapper db, TaskEventHub events, WorkSignals signals) {
        this.signals = signals;
        this.events = events;
        this.db = db;
    }

    @Override
    public void createConversation(String id, Owner owner) {
        db.insertConversation(id, owner);
    }

    @Override
    public String lockConversation(String id, Owner owner) {
        return db.getOwnedConversationForUpdate(id, owner);
    }

    @Override
    public String conversationRelease(String id) {
        return db.getConversationRelease(id);
    }

    @Override
    public String conversationAgent(String id) {
        return db.getConversationAgent(id);
    }

    @Override
    public int bindConversationAgent(String id, String agentId, String releaseHash) {
        return db.updateConversationAgentIfAbsent(id, agentId, releaseHash);
    }

    @Override
    public int bindConversationRelease(String id, String releaseHash) {
        return db.updateConversationReleaseIfAbsent(id, releaseHash);
    }

    @Override
    public Turn replay(String conversation, String key) {
        return db.getTurnByIdempotencyKey(conversation, key);
    }

    @Override
    public void ensureAdmission(Owner owner) {
        db.insertAdmissionIfAbsent(owner);
    }

    @Override
    public boolean admissionPaused(Owner owner) {
        return db.getAdmissionPausedForShare(owner);
    }

    @Override
    public boolean active(String conversation) {
        return db.existsActiveTurnByConversation(conversation);
    }

    @Override
    public void insertTurn(Turn turn) {
        db.insertTurn(turn);
    }

    @Override
    public void recordAdmission(Turn turn, String command) {
        db.insertCommand(command, turn.id());
        db.insertJournalEntryIfAbsent(turn.conversationId(), turn.id(), "user", turn.message());
        events.committed(db.insertTurnEventAndReturn(turn.id()));
        signals.afterCommit(WorkSignals.Queue.COMMAND);
    }
}
