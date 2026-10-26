package com.financeclaw.app.agent.port;

import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.agent.AgentDefinition;
import com.financeclaw.domain.agent.AgentDraft;
import com.financeclaw.domain.agent.ResolvedTarget;
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

    /** 单次集合读取同 owner 的空计划或固定市场计划候选及 profile；选中后仍须核验清单摘要、身份和运行工件。 */
    List<ResolvedTarget> listPublishedLeafTargets(Owner owner);

    default List<com.financeclaw.domain.agent.PrivateAgentTarget> listPublishedReadOnlyTargets(
            Owner owner) {
        return List.of();
    }
}
