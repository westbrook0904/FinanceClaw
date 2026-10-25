package com.financeclaw.infrastructure.release;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.json.JsonMapper;
import com.financeclaw.app.release.port.ReleaseRepository;
import com.financeclaw.app.release.port.StoredRelease;
import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.agent.AgentReleaseManifest;
import com.financeclaw.domain.agent.ComposedAgentReleaseManifest;
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
    public void insertComposedAgentManifestIfAbsent(
            String hash, ComposedAgentReleaseManifest manifest) {
        mapper.insertComposedAgentManifestIfAbsent(hash, PayloadJson.canonical(manifest));
    }

    @Override
    public StoredRelease findStoredRelease(String hash) {
        var row = mapper.getStoredRelease(hash);
        if (row == null) return null;
        try {
            // 物理格式是唯一分派依据，未知格式不尝试其他类型或额外数据库读取。
            if ("legacy-v1".equals(row.manifestFormat())) {
                return new StoredRelease.Legacy(
                        row.releaseHash(), readRoot(row.manifestJson(), ReleaseDefinition.class));
            }
            if ("agent-v2".equals(row.manifestFormat())) {
                return new StoredRelease.AgentV2(
                        row.releaseHash(),
                        readRoot(row.manifestJson(), AgentReleaseManifest.class),
                        readBase(row));
            }
            if ("agent-v3".equals(row.manifestFormat())) {
                return new StoredRelease.AgentV3(
                        row.releaseHash(),
                        readRoot(row.manifestJson(), ComposedAgentReleaseManifest.class),
                        readBase(row));
            }
            throw new Blocked("release_unavailable");
        } catch (JsonProcessingException | IllegalArgumentException invalid) {
            throw new Blocked("release_unavailable");
        }
    }

    private static <T> T readRoot(String json, Class<T> type) throws JsonProcessingException {
        T value = READER.readValue(json, type);
        if (value == null) throw new Blocked("release_unavailable");
        return value;
    }

    /**
     * 基础正文提前随查询返回，但不可提前把它的损坏转换成业务异常。原语义先校验外层 hash、manifest/runtime，再检查基础； 因此这里只标记不可用，由
     * ReleaseService 在原来的校验阶段统一拒绝，不能遮蔽外层更早的错误。
     */
    private static StoredRelease.LegacyBase readBase(ReleaseMapper.StoredReleaseRow row) {
        if (row.baseReleaseHash() == null
                || !"legacy-v1".equals(row.baseManifestFormat())
                || row.baseManifestJson() == null) return null;
        try {
            var definition = READER.readValue(row.baseManifestJson(), ReleaseDefinition.class);
            return definition == null
                    ? null
                    : new StoredRelease.LegacyBase(row.baseReleaseHash(), definition);
        } catch (JsonProcessingException | IllegalArgumentException invalid) {
            return null;
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
