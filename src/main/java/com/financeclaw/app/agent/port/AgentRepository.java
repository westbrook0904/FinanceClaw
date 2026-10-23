package com.financeclaw.app.agent.port;

import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.agent.AgentDefinition;
import com.financeclaw.domain.agent.AgentDraft;
import java.util.List;

/** owner 限定的 Agent 存储；编辑和发布先锁身份行，修订只追加，指针更新使用 CAS。 */
public interface AgentRepository {
    boolean insertIfAbsent(AgentDefinition agent);

    AgentDefinition owned(Owner owner, String id);

    AgentDefinition lockOwned(Owner owner, String id);

    AgentDraft draft(Owner owner, String id, long revision);

    void insertDraft(AgentDraft draft);

    boolean advanceDraft(Owner owner, String id, long expectedRevision);

    List<AgentDefinition> list(Owner owner);
}
