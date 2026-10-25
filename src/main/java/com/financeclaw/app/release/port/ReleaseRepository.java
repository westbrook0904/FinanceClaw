package com.financeclaw.app.release.port;

import com.financeclaw.domain.agent.AgentReleaseManifest;
import com.financeclaw.domain.agent.ComposedAgentReleaseManifest;
import com.financeclaw.domain.release.Deployment;
import com.financeclaw.domain.release.DeploymentChange;
import com.financeclaw.domain.release.ReleaseDefinition;
import java.util.List;

/** 发布持久化端口：定义只插入，指针通过 CAS 更新，指针与变更回执必须处于同一短事务。 */
public interface ReleaseRepository {
    void insertManifestIfAbsent(String hash, ReleaseDefinition release);

    ReleaseDefinition findManifest(String hash);

    /** 同一查询返回选定发布及其单层基础清单；不存在时返回 null，不按格式分别探测或回退。 */
    StoredRelease findStoredRelease(String hash);

    void insertAgentManifestIfAbsent(String hash, AgentReleaseManifest manifest);

    void insertComposedAgentManifestIfAbsent(String hash, ComposedAgentReleaseManifest manifest);

    Deployment current(String deployment);

    boolean insertDeploymentIfAbsent(String deployment, String hash);

    boolean updateDeployment(String deployment, String hash, long expectedRevision);

    void insertChange(DeploymentChange change);

    List<DeploymentChange> history(String deployment, long afterRevision, int limit);
}
