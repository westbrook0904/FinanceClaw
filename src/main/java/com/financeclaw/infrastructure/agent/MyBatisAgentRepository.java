package com.financeclaw.infrastructure.agent;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.json.JsonMapper;
import com.financeclaw.app.agent.port.AgentRepository;
import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.agent.AgentComposition;
import com.financeclaw.domain.agent.AgentDefinition;
import com.financeclaw.domain.agent.AgentDraft;
import com.financeclaw.domain.agent.ResolvedTarget;
import com.financeclaw.support.PayloadJson;
import java.util.List;
import org.springframework.stereotype.Repository;

/** Agent 持久化适配器；事务和 owner 行锁顺序由 AgentService 协调。 */
@Repository
public class MyBatisAgentRepository implements AgentRepository {
    private static final JsonMapper READER = JsonMapper.builder().build();
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
        var row = mapper.getOwnedDraft(owner, id, revision);
        if (row == null) {
            return null;
        }
        try {
            var composition =
                    row.compositionJson() == null
                            ? null
                            : READER.readValue(row.compositionJson(), AgentComposition.class);
            return new AgentDraft(
                    row.agentId(),
                    row.revision(),
                    row.displayName(),
                    row.systemPrompt(),
                    row.baseReleaseHash(),
                    composition);
        } catch (JsonProcessingException | IllegalArgumentException invalid) {
            throw new Blocked("agent_draft_invalid");
        }
    }

    @Override
    public void insertDraft(AgentDraft draft) {
        String composition =
                draft.composition() == null ? null : PayloadJson.canonical(draft.composition());
        if (mapper.insertDraft(draft, composition) != 1) {
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

    @Override
    public List<ResolvedTarget> listPublishedLeafTargets(Owner owner) {
        return mapper.listPublishedLeafTargetsByOwner(owner);
    }
}
