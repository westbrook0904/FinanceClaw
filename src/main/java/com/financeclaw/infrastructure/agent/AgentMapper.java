package com.financeclaw.infrastructure.agent;

import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.agent.AgentDefinition;
import com.financeclaw.domain.agent.AgentDraft;
import java.util.List;
import org.apache.ibatis.annotations.*;

/** Agent 目录 SQL：所有管理读取带 owner，写修订先锁身份行；不存在与跨 owner 使用相同空结果。 */
public interface AgentMapper {
    @Insert(
            """
            INSERT INTO control_plane.agents(id,tenant,subject,draft_revision)
            VALUES(#{id},#{tenant},#{subject},#{draftRevision}) ON CONFLICT(id) DO NOTHING
            """)
    int insertAgentIfAbsent(AgentDefinition agent);

    @Select(
            """
            SELECT id,tenant,subject,draft_revision FROM control_plane.agents
            WHERE id=#{id} AND tenant=#{owner.tenant} AND subject=#{owner.subject}
            """)
    AgentDefinition getOwnedAgent(@Param("owner") Owner owner, @Param("id") String id);

    @Select(
            """
            SELECT id,tenant,subject,draft_revision FROM control_plane.agents
            WHERE id=#{id} AND tenant=#{owner.tenant} AND subject=#{owner.subject} FOR UPDATE
            """)
    AgentDefinition getOwnedAgentForUpdate(@Param("owner") Owner owner, @Param("id") String id);

    @Select(
            """
            SELECT d.agent_id,d.revision,d.display_name,d.system_prompt,d.base_release_hash
            FROM control_plane.agent_draft_revisions d JOIN control_plane.agents a ON a.id=d.agent_id
            WHERE a.id=#{id} AND a.tenant=#{owner.tenant} AND a.subject=#{owner.subject}
              AND d.revision=#{revision}
            """)
    AgentDraft getOwnedDraft(
            @Param("owner") Owner owner, @Param("id") String id, @Param("revision") long revision);

    @Insert(
            """
            INSERT INTO control_plane.agent_draft_revisions
              (agent_id,revision,display_name,system_prompt,base_release_hash)
            VALUES(#{agentId},#{revision},#{displayName},#{systemPrompt},#{baseReleaseHash})
            """)
    int insertDraft(AgentDraft draft);

    @Update(
            """
            UPDATE control_plane.agents SET draft_revision=draft_revision+1
            WHERE id=#{id} AND tenant=#{owner.tenant} AND subject=#{owner.subject}
              AND draft_revision=#{revision}
            """)
    int updateDraftRevision(
            @Param("owner") Owner owner, @Param("id") String id, @Param("revision") long revision);

    @Select(
            """
            SELECT id,tenant,subject,draft_revision FROM control_plane.agents
            WHERE tenant=#{owner.tenant} AND subject=#{owner.subject} ORDER BY created_at,id
            """)
    List<AgentDefinition> listOwnedAgents(@Param("owner") Owner owner);
}
