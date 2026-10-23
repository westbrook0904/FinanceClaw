package com.financeclaw.app.artifact.port;

import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.Turn;

/** 产物存储边界：保存完整载荷及身份、权限元数据，读取时仍由应用层进行隐私和授权检查。 */
public interface ArtifactGateway {
    Artifact artifact(String id);

    int insertArtifact(Artifact artifact);

    String artifactScope(String id);

    int setArtifactScope(String id, String scope);

    Policy policy(String turn);

    Turn turn(String id);

    Grant lockGrant(String conversation);
}
