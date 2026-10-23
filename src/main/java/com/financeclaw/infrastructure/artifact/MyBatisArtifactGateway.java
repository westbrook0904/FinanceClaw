package com.financeclaw.infrastructure.artifact;

import com.financeclaw.app.artifact.port.ArtifactGateway;
import com.financeclaw.domain.Governance.*;
import com.financeclaw.domain.Task.Turn;
import com.financeclaw.infrastructure.governance.GovernanceMapper;
import com.financeclaw.infrastructure.task.TaskMapper;
import org.springframework.stereotype.Repository;

/**
 * 实现 ArtifactGateway，提供完整产物、读取权限以及关联 Turn 和授权的数据库访问。 归档正文与 required scope 在 ArtifactService
 * 的事务中保存，读取时由用例验证 owner、隐私和当前授权。按 ID 查到记录本身不代表调用者可以访问。
 */
@Repository
public class MyBatisArtifactGateway implements ArtifactGateway {
    private final GovernanceMapper db;
    private final TaskMapper turns;

    public MyBatisArtifactGateway(GovernanceMapper db, TaskMapper turns) {
        this.db = db;
        this.turns = turns;
    }

    @Override
    public Artifact artifact(String id) {
        return db.getArtifactById(id);
    }

    @Override
    public int insertArtifact(Artifact artifact) {
        return db.insertArtifact(artifact);
    }

    @Override
    public String artifactScope(String id) {
        return db.getArtifactScope(id);
    }

    @Override
    public int setArtifactScope(String id, String scope) {
        return db.updateArtifactScope(id, scope);
    }

    @Override
    public Policy policy(String turn) {
        return db.getTurnPolicy(turn);
    }

    @Override
    public Turn turn(String id) {
        return turns.getTurnById(id);
    }

    @Override
    public Grant lockGrant(String conversation) {
        return db.getGrantForUpdate(conversation);
    }
}
