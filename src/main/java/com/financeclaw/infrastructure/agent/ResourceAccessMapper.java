package com.financeclaw.infrastructure.agent;

import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.authorization.ResourceGrant;
import java.util.List;
import org.apache.ibatis.annotations.*;

/** 精确资源授权与审计；CAS 更新和审计追加由同一事务提交。 */
public interface ResourceAccessMapper {
    @Select("SELECT tenant,subject FROM control_plane.agents WHERE id=#{id} AND NOT archived")
    Owner agentOwner(String id);

    @Select(
            "SELECT tenant,subject FROM control_plane.agents WHERE id=#{id} AND NOT archived FOR SHARE")
    Owner agentOwnerForShare(String id);

    @Update(
            """
        UPDATE control_plane.agents SET archived=true
        WHERE id=#{id} AND tenant=#{owner.tenant} AND subject=#{owner.subject}
          AND draft_revision=#{revision} AND NOT archived
        """)
    int archive(
            @Param("owner") Owner owner, @Param("id") String id, @Param("revision") long revision);

    @Select(
            """
        SELECT resource_kind,resource_id,resource_tenant,resource_subject,
          grantee_tenant,grantee_subject,release_hash,binding_hash,revision,enabled
        FROM control_plane.resource_grants
        WHERE resource_kind=#{kind} AND resource_id=#{id}
          AND grantee_tenant=#{grantee.tenant} AND grantee_subject=#{grantee.subject}
        """)
    ResourceGrant grant(
            @Param("kind") String kind, @Param("id") String id, @Param("grantee") Owner grantee);

    @Select(
            """
        SELECT resource_kind,resource_id,resource_tenant,resource_subject,
          grantee_tenant,grantee_subject,release_hash,binding_hash,revision,enabled
        FROM control_plane.resource_grants
        WHERE resource_kind=#{kind} AND resource_id=#{id}
          AND grantee_tenant=#{grantee.tenant} AND grantee_subject=#{grantee.subject}
        FOR SHARE
        """)
    ResourceGrant grantForShare(
            @Param("kind") String kind, @Param("id") String id, @Param("grantee") Owner grantee);

    @Select(
            """
        SELECT resource_kind,resource_id,resource_tenant,resource_subject,
          grantee_tenant,grantee_subject,release_hash,binding_hash,revision,enabled
        FROM control_plane.resource_grants
        WHERE resource_kind=#{kind} AND resource_id=#{id}
          AND resource_tenant=#{owner.tenant} AND resource_subject=#{owner.subject}
        ORDER BY grantee_tenant,grantee_subject LIMIT 1001
        """)
    List<ResourceGrant> grants(
            @Param("kind") String kind, @Param("id") String id, @Param("owner") Owner owner);

    @Select(
            """
        SELECT g.resource_kind,g.resource_id,g.resource_tenant,g.resource_subject,
          g.grantee_tenant,g.grantee_subject,g.release_hash,g.binding_hash,g.revision,g.enabled
        FROM control_plane.resource_grants g
        JOIN control_plane.agents a ON a.id=g.resource_id AND NOT a.archived
          AND a.tenant=g.resource_tenant AND a.subject=g.resource_subject
        WHERE g.resource_kind='agent' AND g.enabled
          AND g.grantee_tenant=#{caller.tenant} AND g.grantee_subject=#{caller.subject}
        ORDER BY g.resource_id LIMIT 1001
        """)
    List<ResourceGrant> sharedAgents(@Param("caller") Owner caller);

    @Insert(
            """
        INSERT INTO control_plane.resource_grants(resource_kind,resource_id,resource_tenant,
          resource_subject,grantee_tenant,grantee_subject,release_hash,binding_hash,revision,enabled)
        VALUES(#{resourceKind},#{resourceId},#{resourceTenant},#{resourceSubject},
          #{granteeTenant},#{granteeSubject},#{releaseHash},#{bindingHash},#{revision},#{enabled})
        ON CONFLICT DO NOTHING
        """)
    int insert(ResourceGrant grant);

    @Update(
            """
        UPDATE control_plane.resource_grants
        SET release_hash=#{grant.releaseHash},binding_hash=#{grant.bindingHash},revision=#{grant.revision},
          enabled=#{grant.enabled},changed_at=clock_timestamp()
        WHERE resource_kind=#{grant.resourceKind} AND resource_id=#{grant.resourceId}
          AND resource_tenant=#{grant.resourceTenant} AND resource_subject=#{grant.resourceSubject}
          AND grantee_tenant=#{grant.granteeTenant} AND grantee_subject=#{grant.granteeSubject}
          AND revision=#{expected}
        """)
    int update(@Param("grant") ResourceGrant grant, @Param("expected") long expected);

    @Insert(
            """
        INSERT INTO control_plane.resource_grant_audit(resource_kind,resource_id,resource_tenant,
          resource_subject,grantee_tenant,grantee_subject,release_hash,binding_hash,revision,enabled)
        VALUES(#{resourceKind},#{resourceId},#{resourceTenant},#{resourceSubject},
          #{granteeTenant},#{granteeSubject},#{releaseHash},#{bindingHash},#{revision},#{enabled})
        """)
    int audit(ResourceGrant grant);

    @Select(
            """
        SELECT resource_kind,resource_id,resource_tenant,resource_subject,
          grantee_tenant,grantee_subject,release_hash,binding_hash,revision,enabled
        FROM control_plane.resource_grant_audit
        WHERE resource_kind=#{kind} AND resource_id=#{id}
          AND resource_tenant=#{owner.tenant} AND resource_subject=#{owner.subject}
          AND grantee_tenant=#{grantee.tenant} AND grantee_subject=#{grantee.subject}
          AND revision > #{after}
        ORDER BY revision LIMIT #{limit}
        """)
    List<ResourceGrant> history(
            @Param("kind") String kind,
            @Param("id") String id,
            @Param("owner") Owner owner,
            @Param("grantee") Owner grantee,
            @Param("after") long after,
            @Param("limit") int limit);
}
