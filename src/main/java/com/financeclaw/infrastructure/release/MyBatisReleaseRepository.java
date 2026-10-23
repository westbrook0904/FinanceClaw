package com.financeclaw.infrastructure.release;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.json.JsonMapper;
import com.financeclaw.app.release.port.ReleaseRepository;
import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.agent.AgentReleaseManifest;
import com.financeclaw.domain.release.Deployment;
import com.financeclaw.domain.release.DeploymentChange;
import com.financeclaw.domain.release.ReleaseDefinition;
import com.financeclaw.support.PayloadJson;
import java.util.List;
import org.springframework.boot.sql.init.dependency.DependsOnDatabaseInitialization;
import org.springframework.stereotype.Repository;

/** 持久发布适配器；先完成 Flyway 初始化才允许服务执行 bootstrap，不提供进程内降级目录。 */
@Repository
@DependsOnDatabaseInitialization
public class MyBatisReleaseRepository implements ReleaseRepository {
    private static final JsonMapper READER = JsonMapper.builder().build();
    private final ReleaseMapper mapper;

    public MyBatisReleaseRepository(ReleaseMapper mapper) {
        this.mapper = mapper;
    }

    @Override
    public void insertManifestIfAbsent(String hash, ReleaseDefinition release) {
        mapper.insertManifestIfAbsent(hash, PayloadJson.canonical(release));
    }

    @Override
    public ReleaseDefinition findManifest(String hash) {
        String json = mapper.getManifest(hash);
        if (json == null) {
            return null;
        }
        try {
            return READER.readValue(json, ReleaseDefinition.class);
        } catch (JsonProcessingException | IllegalArgumentException invalid) {
            throw new Blocked("release_unavailable");
        }
    }

    @Override
    public void insertAgentManifestIfAbsent(String hash, AgentReleaseManifest manifest) {
        mapper.insertAgentManifestIfAbsent(hash, PayloadJson.canonical(manifest));
    }

    @Override
    public AgentReleaseManifest findAgentManifest(String hash) {
        String json = mapper.getAgentManifest(hash);
        if (json == null) {
            return null;
        }
        try {
            return READER.readValue(json, AgentReleaseManifest.class);
        } catch (JsonProcessingException | IllegalArgumentException invalid) {
            throw new Blocked("release_unavailable");
        }
    }

    @Override
    public Deployment current(String deployment) {
        return mapper.getDeployment(deployment);
    }

    @Override
    public boolean insertDeploymentIfAbsent(String deployment, String hash) {
        return mapper.insertDeploymentIfAbsent(deployment, hash) == 1;
    }

    @Override
    public boolean updateDeployment(String deployment, String hash, long expectedRevision) {
        return mapper.updateDeploymentByRevision(deployment, hash, expectedRevision) == 1;
    }

    @Override
    public void insertChange(DeploymentChange change) {
        if (mapper.insertChange(change) != 1) {
            throw new IllegalStateException("Release change was not persisted");
        }
    }

    @Override
    public List<DeploymentChange> history(String deployment, long afterRevision, int limit) {
        return mapper.listChanges(deployment, afterRevision, limit);
    }
}
