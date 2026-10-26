package com.financeclaw.app.task;

import com.financeclaw.app.execution.port.*;
import com.financeclaw.app.task.port.*;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;
import com.financeclaw.domain.agent.AgentDraft;
import java.util.*;
import org.springframework.stereotype.Service;

/** 查询已鉴权的会话与任务；所有资源读取先核对租户和用户归属，避免通过资源编号跨用户访问。 */
@Service
public class TaskQueryService {
    private final TaskQueryGateway db;
    private com.financeclaw.app.agent.port.AgentAccessGateway agents;
    private com.financeclaw.app.release.ReleaseService releases;
    private org.springframework.transaction.support.TransactionTemplate tx;

    public void configureResultAccess(
            com.financeclaw.app.agent.port.AgentAccessGateway agents,
            com.financeclaw.app.release.ReleaseService releases,
            org.springframework.transaction.support.TransactionTemplate tx) {
        this.agents = agents;
        this.releases = releases;
        this.tx = tx;
    }

    private void requireResult(Turn turn) {
        if (agents == null)
            return; // Explicit in-memory fixtures; production assembly configures the port.
        // Builtin format-1 releases cannot select Agent capabilities. Admission freezes this
        // identity;
        // their historical reads need no additional shared-Agent source authorization.
        if (AgentDraft.BUILTIN_AGENT.equals(turn.agentId())) return;
        tx.executeWithoutResult(
                status -> {
                    try {
                        com.financeclaw.app.agent.AgentResultAccess.require(
                                agents,
                                releases,
                                new Owner(turn.tenant(), turn.subject()),
                                turn.agentId(),
                                turn.releaseHash());
                    } catch (Blocked denied) {
                        throw new Problem(403, denied.getMessage());
                    }
                });
    }

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
        if (t.result() != null) requireResult(t);
        return t;
    }

    public List<Journal> messages(Owner owner, String conversation, long after, int limit) {
        owned(owner, conversation);
        if (after < 0 || limit < 1 || limit > 500) {
            throw new Problem(422, "invalid_pagination");
        }
        var rows = db.messages(conversation, after, limit);
        var checked = new HashSet<String>();
        for (var message : rows) {
            if (agents == null
                    || !message.role().equals("assistant")
                    || !checked.add(message.turnId())) continue;
            var turn = db.ownedTurn(owner, conversation, message.turnId());
            if (turn == null) throw new Problem(404, "not_found");
            requireResult(turn);
        }
        return rows;
    }

    /** 复用不可变任务身份，并在每次有界补读前重新核验结果来源授权。 */
    public List<Event> replay(Turn authorized, long after) {
        requireResult(authorized);
        return db.events(authorized.id(), after);
    }

    public List<Event> events(Owner owner, String conversation, String turn, long after) {
        Turn t = get(owner, conversation, turn);
        if (after < 0 || after > t.revision()) {
            throw new Problem(422, "invalid_event_cursor");
        }
        requireResult(t);
        return db.events(turn, after);
    }
}
