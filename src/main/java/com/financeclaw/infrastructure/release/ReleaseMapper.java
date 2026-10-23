package com.financeclaw.infrastructure.release;

import com.financeclaw.domain.release.Deployment;
import com.financeclaw.domain.release.DeploymentChange;
import java.util.List;
import org.apache.ibatis.annotations.*;

/** 发布目录 SQL：定义和历史只追加，指针 CAS 在应用短事务中与历史记录共同提交。 */
public interface ReleaseMapper {
    @Insert(
            """
            INSERT INTO control_plane.release_manifests(release_hash,manifest)
            VALUES(#{hash},CAST(#{manifest} AS jsonb)) ON CONFLICT(release_hash) DO NOTHING
            """)
    int insertManifestIfAbsent(@Param("hash") String hash, @Param("manifest") String manifest);

    @Select("SELECT manifest::text FROM control_plane.release_manifests WHERE release_hash=#{hash}")
    String getManifest(String hash);

    @Select("SELECT name,release_hash,revision FROM control_plane.deployments WHERE name=#{name}")
    Deployment getDeployment(String name);

    @Insert(
            """
            INSERT INTO control_plane.deployments(name,release_hash,revision)
            VALUES(#{name},#{hash},1) ON CONFLICT(name) DO NOTHING
            """)
    int insertDeploymentIfAbsent(@Param("name") String name, @Param("hash") String hash);

    /** revision 条件即写入授权；失败不能自动重试并覆盖另一发布者的决定。 */
    @Update(
            """
            UPDATE control_plane.deployments
            SET release_hash=#{hash},revision=revision+1,updated_at=clock_timestamp()
            WHERE name=#{name} AND revision=#{revision}
            """)
    int updateDeploymentByRevision(
            @Param("name") String name,
            @Param("hash") String hash,
            @Param("revision") long revision);

    @Insert(
            """
            INSERT INTO control_plane.deployment_changes(deployment,revision,release_hash,previous_hash)
            VALUES(#{deployment},#{revision},#{releaseHash},#{previousHash})
            """)
    int insertChange(DeploymentChange change);

    @Select(
            """
            SELECT deployment,revision,release_hash,previous_hash
            FROM control_plane.deployment_changes
            WHERE deployment=#{name} AND revision>#{after}
            ORDER BY revision LIMIT #{limit}
            """)
    List<DeploymentChange> listChanges(
            @Param("name") String name, @Param("after") long after, @Param("limit") int limit);
}
