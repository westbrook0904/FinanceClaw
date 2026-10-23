package com.financeclaw.app.task.port;

import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.*;
import java.util.List;

/** 任务查询与归属验证边界：持久化实现负责租户、用户、会话与任务的一致关联。 */
public interface TaskQueryGateway {
    String ownedConversation(String id, Owner owner);

    Turn turn(String id);

    Turn ownedTurn(Owner owner, String conversation, String turn);

    List<Journal> messages(String conversation, long after, int limit);

    List<Event> events(String turn, long after);

    List<Turn> history(String conversation, String release, long revision);
}
