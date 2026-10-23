package com.financeclaw.infrastructure.task;

import com.financeclaw.app.task.port.TaskQueryGateway;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;
import java.util.List;
import org.springframework.stereotype.Repository;

/**
 * 实现 TaskQueryGateway，提供 owner 范围的会话任务读取及有游标的历史查询。 外部请求先通过 owned 方法验证身份，内部按 ID 读取不自行补充权限。历史按
 * release 和 grant revision 过滤，记忆用例再检查隐私 epoch；模型上下文由独立工作投影接续。
 */
@Repository
public class MyBatisTaskQueryGateway implements TaskQueryGateway {
    private final TaskMapper db;

    public MyBatisTaskQueryGateway(TaskMapper db) {
        this.db = db;
    }

    @Override
    public String ownedConversation(String id, Owner owner) {
        return db.getOwnedConversation(id, owner);
    }

    @Override
    public Turn ownedTurn(Owner owner, String conversation, String turn) {
        return db.getOwnedTurn(owner, conversation, turn);
    }

    @Override
    public Turn turn(String id) {
        return db.getTurnById(id);
    }

    @Override
    public List<Journal> messages(String conversation, long after, int limit) {
        return db.listJournalEntriesAfterSequence(conversation, after, limit);
    }

    @Override
    public List<Event> events(String turn, long after) {
        return db.listTurnEventsAfterRevision(turn, after);
    }

    @Override
    public List<Turn> history(String conversation, String release, long revision) {
        return db.listCompletedHistoryByReleaseAndGrant(conversation, release, revision);
    }
}
