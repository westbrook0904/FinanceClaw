package com.financeclaw.infrastructure.agent;

import com.financeclaw.app.agent.port.AgentRepository;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.agent.AgentDefinition;
import com.financeclaw.domain.agent.AgentDraft;
import java.util.List;
import org.springframework.stereotype.Repository;

/** Agent 持久化适配器；事务和 owner 行锁顺序由 AgentService 协调。 */
@Repository
public class MyBatisAgentRepository implements AgentRepository {
    private final AgentMapper mapper;

    public MyBatisAgentRepository(AgentMapper mapper) {
        this.mapper = mapper;
    }

    @Override
    public boolean insertIfAbsent(AgentDefinition agent) {
        return mapper.insertAgentIfAbsent(agent) == 1;
    }

    @Override
    public AgentDefinition owned(Owner owner, String id) {
        return mapper.getOwnedAgent(owner, id);
    }

    @Override
    public AgentDefinition lockOwned(Owner owner, String id) {
        return mapper.getOwnedAgentForUpdate(owner, id);
    }

    @Override
    public AgentDraft draft(Owner owner, String id, long revision) {
        return mapper.getOwnedDraft(owner, id, revision);
    }

    @Override
    public void insertDraft(AgentDraft draft) {
        if (mapper.insertDraft(draft) != 1) {
            throw new IllegalStateException("Agent draft was not persisted");
        }
    }

    @Override
    public boolean advanceDraft(Owner owner, String id, long expectedRevision) {
        return mapper.updateDraftRevision(owner, id, expectedRevision) == 1;
    }

    @Override
    public List<AgentDefinition> list(Owner owner) {
        return mapper.listOwnedAgents(owner);
    }
}
