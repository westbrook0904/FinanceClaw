package com.financeclaw.infrastructure.agent;

import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.agent.AgentDefinition;
import com.financeclaw.domain.agent.AgentDraft;
import com.financeclaw.domain.agent.PrivateAgentTarget;
import com.financeclaw.domain.agent.ResolvedTarget;
import java.util.List;
import org.apache.ibatis.annotations.*;

/** Agent 目录 SQL：所有管理读取带 owner，写修订先锁身份行；不存在与跨 owner 使用相同空结果。 */
public interface AgentMapper {
    record DraftRow(
            String agentId,
            long revision,
            String displayName,
            String systemPrompt,
            String baseReleaseHash,
            String compositionJson) {}

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
            SELECT d.agent_id,d.revision,d.display_name,d.system_prompt,d.base_release_hash,
              d.composition::text AS composition_json
            FROM control_plane.agent_draft_revisions d JOIN control_plane.agents a ON a.id=d.agent_id
            WHERE a.id=#{id} AND a.tenant=#{owner.tenant} AND a.subject=#{owner.subject}
              AND d.revision=#{revision}
            """)
    DraftRow getOwnedDraft(
            @Param("owner") Owner owner, @Param("id") String id, @Param("revision") long revision);

    @Insert(
            """
            INSERT INTO control_plane.agent_draft_revisions
              (agent_id,revision,display_name,system_prompt,base_release_hash,composition)
            VALUES(#{draft.agentId},#{draft.revision},#{draft.displayName},#{draft.systemPrompt},#{draft.baseReleaseHash},CAST(#{composition} AS jsonb))
            """)
    int insertDraft(@Param("draft") AgentDraft draft, @Param("composition") String composition);

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

    /**
     * 单次 owner JOIN 读取候选与 profile，不为每个 Agent 读取清单。只接受显式 v3 空计划或固定市场闭包， 不能把缺失计划当成空工具；这里只筛选元数据，选中后仍由
     * ReleaseService 和领域规则校验完整清单。
     */
    @Select(
            """
            SELECT agent_id,release_hash,profile FROM (
              SELECT a.id AS agent_id,d.release_hash,a.created_at,
                CASE
                  WHEN m.manifest#>'{plan,requestedCapabilities}'='[]'::jsonb
                    AND m.manifest#>'{plan,capabilities}'='[]'::jsonb
                    AND m.manifest#>'{plan,definition,scopes}'='[]'::jsonb THEN 'EMPTY'
                  WHEN m.manifest#>'{plan,requestedCapabilities}'='["function:market_snapshot"]'::jsonb
                    AND jsonb_array_length(CASE
                      WHEN jsonb_typeof(m.manifest#>'{plan,capabilities}')='array'
                      THEN m.manifest#>'{plan,capabilities}' ELSE '[]'::jsonb END)=2
                    AND m.manifest#>>'{plan,capabilities,0,id}'='function:market_snapshot'
                    AND m.manifest#>>'{plan,capabilities,0,kind}'='function'
                    AND m.manifest#>>'{plan,capabilities,1,id}'='function:read_artifact_range'
                    AND m.manifest#>>'{plan,capabilities,1,kind}'='function'
                    AND m.manifest#>'{plan,definition,scopes}'='["market:read"]'::jsonb
                    THEN 'MARKET_SNAPSHOT'
                END AS profile
              FROM control_plane.agents a
              JOIN control_plane.deployments d ON d.name='agent:' || a.id
              JOIN control_plane.release_manifests m ON m.release_hash=d.release_hash
              WHERE a.tenant=#{owner.tenant} AND a.subject=#{owner.subject}
                AND m.manifest_format='agent-v3'
                AND m.manifest->>'format'='agent-release/3'
                AND m.manifest->>'agentId'=a.id
                AND m.manifest->>'tenant'=a.tenant AND m.manifest->>'subject'=a.subject
            ) candidates WHERE profile IS NOT NULL ORDER BY created_at,agent_id
            """)
    @ConstructorArgs({
        @Arg(column = "agent_id", javaType = String.class),
        @Arg(column = "release_hash", javaType = String.class),
        @Arg(column = "profile", javaType = PrivateAgentTarget.Profile.class)
    })
    List<ResolvedTarget> listPublishedLeafTargetsByOwner(@Param("owner") Owner owner);
}
