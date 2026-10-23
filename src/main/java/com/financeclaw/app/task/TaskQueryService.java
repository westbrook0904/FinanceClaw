package com.financeclaw.app.task;

import com.financeclaw.app.execution.port.*;
import com.financeclaw.app.task.port.*;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;
import java.util.*;
import org.springframework.stereotype.Service;

/** 查询已鉴权的会话与任务；所有资源读取先核对租户和用户归属，避免通过资源编号跨用户访问。 */
@Service
public class TaskQueryService {
    private final TaskQueryGateway db;

    public TaskQueryService(TaskQueryGateway db) {
        this.db = db;
    }

    public void owned(Owner owner, String conversation) {
        if (db.ownedConversation(conversation, owner) == null) {
            throw new Problem(404, "not_found");
        }
    }

    public Turn get(Owner owner, String conversation, String turn) {
        Turn t = db.ownedTurn(owner, conversation, turn);
        if (t == null || !t.conversationId().equals(conversation)) {
            throw new Problem(404, "not_found");
        }
        return t;
    }

    public List<Journal> messages(Owner owner, String conversation, long after, int limit) {
        owned(owner, conversation);
        if (after < 0 || limit < 1 || limit > 500) {
            throw new Problem(422, "invalid_pagination");
        }
        return db.messages(conversation, after, limit);
    }

    /** 复用已经鉴权的不可变任务身份，每页只执行一次有界查询。 */
    public List<Event> replay(Turn authorized, long after) {
        return db.events(authorized.id(), after);
    }

    public List<Event> events(Owner owner, String conversation, String turn, long after) {
        Turn t = get(owner, conversation, turn);
        if (after < 0 || after > t.revision()) {
            throw new Problem(422, "invalid_event_cursor");
        }
        return db.events(turn, after);
    }
}
