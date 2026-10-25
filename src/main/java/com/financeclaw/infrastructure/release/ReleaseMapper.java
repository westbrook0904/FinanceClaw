package com.financeclaw.infrastructure.release;

import com.financeclaw.domain.release.Deployment;
import com.financeclaw.domain.release.DeploymentChange;
import java.util.List;
import org.apache.ibatis.annotations.*;

/** 发布目录 SQL：定义和历史只追加，指针 CAS 在应用短事务中与历史记录共同提交。 */
public interface ReleaseMapper {
    /** 显式列顺序对应 record 构造器；不复用已经丢失基础身份的执行投影。 */
    record StoredReleaseRow(
            String releaseHash,
            String manifestFormat,
            String manifestJson,
            String baseReleaseHash,
            String baseManifestFormat,
            String baseManifestJson) {}

    @Insert(
            """
            INSERT INTO control_plane.release_manifests(release_hash,manifest)
            VALUES(#{hash},CAST(#{manifest} AS jsonb)) ON CONFLICT(release_hash) DO NOTHING
            """)
    int insertManifestIfAbsent(@Param("hash") String hash, @Param("manifest") String manifest);

    @Select(
            "SELECT manifest::text FROM control_plane.release_manifests WHERE release_hash=#{hash} AND manifest_format='legacy-v1'")
    String getManifest(String hash);

    /**
     * 根与基础均按 release_hash 主键定位，一次 JDBC 查询最多返回一行。LEFT JOIN 保留缺失/错误基础的根行， 不能将基础格式条件移到 WHERE
     * 而提前吞掉根清单。此处只读取事实，不递归、不按 current 指针替代历史 hash。
     */
    @Select(
            """
            SELECT r.release_hash, r.manifest_format, r.manifest::text AS manifest_json,
                   b.release_hash AS base_release_hash,
                   b.manifest_format AS base_manifest_format,
                   b.manifest::text AS base_manifest_json
            FROM control_plane.release_manifests r
            LEFT JOIN control_plane.release_manifests b
              ON r.manifest_format IN ('agent-v2','agent-v3')
             AND b.release_hash=r.manifest->>'baseReleaseHash'
            WHERE r.release_hash=#{hash}
            """)
    StoredReleaseRow getStoredRelease(String hash);

    @Insert(
            """
            INSERT INTO control_plane.release_manifests(release_hash,manifest,manifest_format)
            VALUES(#{hash},CAST(#{manifest} AS jsonb),'agent-v2')
            ON CONFLICT(release_hash) DO NOTHING
            """)
    int insertAgentManifestIfAbsent(@Param("hash") String hash, @Param("manifest") String manifest);

    @Insert(
            """
            INSERT INTO control_plane.release_manifests(release_hash,manifest,manifest_format)
            VALUES(#{hash},CAST(#{manifest} AS jsonb),'agent-v3')
            ON CONFLICT(release_hash) DO NOTHING
            """)
    int insertComposedAgentManifestIfAbsent(
            @Param("hash") String hash, @Param("manifest") String manifest);

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
