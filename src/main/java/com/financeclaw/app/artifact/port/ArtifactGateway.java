package com.financeclaw.app.artifact.port;

import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.Turn;

/** 产物存储边界：保存完整载荷及身份、权限元数据，读取时仍由应用层进行隐私和授权检查。 */
public interface ArtifactGateway {
    Artifact artifact(String id);

    int insertArtifact(Artifact artifact);

    /** 读取权限和隐私标记共享一次查询，避免每种产物读取方式分别增加数据库往返。 */
    record Access(String requiredScope, boolean privacyBound) {}

    Access artifactAccess(String id);

    /** 工具结果的正文、原始工具权限和隐私绑定一次原子插入。 */
    int insertToolResultArtifact(Artifact artifact, String requiredScope);

    int setArtifactScope(String id, String scope);

    Policy policy(String turn);

    Turn turn(String id);

    Grant lockGrant(String conversation);
}
