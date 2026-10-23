package com.financeclaw.app.release;

import com.financeclaw.app.release.port.ReleaseRepository;
import com.financeclaw.app.release.port.ReleaseSource;
import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.Task.Problem;
import com.financeclaw.domain.agent.AgentDraft;
import com.financeclaw.domain.agent.AgentReleaseManifest;
import com.financeclaw.domain.agent.ResolvedAgentRelease;
import com.financeclaw.domain.release.Deployment;
import com.financeclaw.domain.release.DeploymentChange;
import com.financeclaw.domain.release.ReleaseDefinition;
import java.util.List;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionTemplate;

/** 持久发布目录：启动只初始化缺失指针，显式发布使用 CAS；历史任务始终按固定摘要读取定义。 */
@Service
public class ReleaseService {
    public static final String DEFAULT_DEPLOYMENT = "default";
    private static final int MAX_HISTORY_PAGE = 100;
    private final ReleaseRepository repository;
    private final TransactionTemplate tx;

    public ReleaseService(
            ReleaseSource source, ReleaseRepository repository, TransactionTemplate tx) {
        this(repository, tx);
        var initial = source.initial();
        tx.executeWithoutResult(
                status -> {
                    String hash = registerDefinition(initial);
                    // 首次启动的竞争由唯一键裁决；配置变化只登记定义，不覆盖数据库中的指针。
                    if (repository.insertDeploymentIfAbsent(DEFAULT_DEPLOYMENT, hash)) {
                        repository.insertChange(
                                new DeploymentChange(DEFAULT_DEPLOYMENT, 1, hash, null));
                    }
                    current();
                });
    }

    private ReleaseService(ReleaseRepository repository, TransactionTemplate tx) {
        this.repository = repository;
        this.tx = tx;
    }

    /** 打开既有目录供查询或回滚；不读取启动配置、不创建缺失指针，也不自动登记新版本。 */
    public static ReleaseService open(ReleaseRepository repository, TransactionTemplate tx) {
        return new ReleaseService(repository, tx);
    }

    public String currentHash() {
        return current().releaseHash();
    }

    public Deployment current() {
        var deployment = repository.current(DEFAULT_DEPLOYMENT);
        if (deployment == null) {
            throw new Blocked("release_unavailable");
        }
        requireLegacy(deployment.releaseHash());
        return deployment;
    }

    /** 登记不可变定义而不改变执行入口，供显式发布和恢复历史定义使用。 */
    public String register(ReleaseDefinition release) {
        return tx.execute(status -> registerDefinition(release));
    }

    private String registerDefinition(ReleaseDefinition release) {
        release.validate();
        String hash = ReleaseIdentity.hash(release);
        repository.insertManifestIfAbsent(hash, release);
        // ON CONFLICT 不代表已有内容可信；每次重新核验摘要，不能用新定义修补损坏的旧记录。
        requireLegacy(hash);
        return hash;
    }

    /** 注册新格式 Agent 发布，基础清单必须是旧格式，禁止递归引用可变配置或其他 Agent。 */
    public String registerAgent(AgentReleaseManifest manifest) {
        return tx.execute(
                status -> {
                    manifest.validate();
                    requireLegacy(manifest.baseReleaseHash());
                    String hash = AgentReleaseIdentity.hash(manifest);
                    repository.insertAgentManifestIfAbsent(hash, manifest);
                    var stored = resolve(hash);
                    if (!stored.agentId().equals(manifest.agentId())) {
                        throw new Blocked("agent_identity_mismatch");
                    }
                    return hash;
                });
    }

    /** 显式发布的兼容入口；并发切换必须报告冲突，不重读新 revision 后静默覆盖。 */
    public void publish(ReleaseDefinition release) {
        String hash = register(release);
        activate(hash, current().revision());
    }

    /** CAS 切换指针并记录不可变历史；回滚到旧摘要也递增 revision，已有 Turn 不受影响。 */
    public Deployment activate(String hash, long expectedRevision) {
        if (expectedRevision < 1 || expectedRevision == Long.MAX_VALUE) {
            throw new Problem(422, "invalid_release_revision");
        }
        return tx.execute(
                status -> {
                    // 全局入口只接收 legacy 发布，不能把 owner 私有 Agent 暴露为默认 Agent。
                    requireLegacy(hash);
                    return switchDeployment(DEFAULT_DEPLOYMENT, hash, expectedRevision);
                });
    }

    /** 第一次发布使用 expectedRevision=0；后续切换与回滚沿用同一 CAS 和不可变历史。 */
    public Deployment activateForAgent(String agentId, String hash, long expectedRevision) {
        String name = agentDeployment(agentId);
        if (expectedRevision < 0 || expectedRevision == Long.MAX_VALUE) {
            throw new Problem(422, "invalid_release_revision");
        }
        return tx.execute(
                status -> {
                    var release = resolve(hash);
                    if (!agentId.equals(release.agentId()) || release.tenant() == null) {
                        throw new Problem(409, "agent_release_mismatch");
                    }
                    if (expectedRevision == 0) {
                        if (!repository.insertDeploymentIfAbsent(name, hash)) {
                            throw new Problem(409, "release_revision_changed");
                        }
                        repository.insertChange(new DeploymentChange(name, 1, hash, null));
                        return new Deployment(name, hash, 1);
                    }
                    return switchDeployment(name, hash, expectedRevision);
                });
    }

    private Deployment switchDeployment(String name, String hash, long expectedRevision) {
        var previous = repository.current(name);
        if (previous == null
                || previous.revision() != expectedRevision
                || !repository.updateDeployment(name, hash, expectedRevision)) {
            throw new Problem(409, "release_revision_changed");
        }
        var next = new Deployment(name, hash, expectedRevision + 1);
        repository.insertChange(
                new DeploymentChange(name, next.revision(), hash, previous.releaseHash()));
        return next;
    }

    /** 未发布返回 null；存在指针但清单身份不匹配时阻断，不能伪装成未发布。 */
    public Deployment currentForAgent(String agentId) {
        var deployment = repository.current(agentDeployment(agentId));
        if (deployment != null && !resolve(deployment.releaseHash()).agentId().equals(agentId)) {
            throw new Blocked("agent_identity_mismatch");
        }
        return deployment;
    }

    public List<DeploymentChange> historyForAgent(String agentId, long afterRevision, int limit) {
        String name = agentDeployment(agentId);
        requireHistoryPage(afterRevision, limit);
        return List.copyOf(repository.history(name, afterRevision, limit));
    }

    private static String agentDeployment(String agentId) {
        AgentDraft.requireCustomId(agentId);
        return "agent:" + agentId;
    }

    public List<DeploymentChange> history(long afterRevision, int limit) {
        requireHistoryPage(afterRevision, limit);
        return List.copyOf(repository.history(DEFAULT_DEPLOYMENT, afterRevision, limit));
    }

    private static void requireHistoryPage(long afterRevision, int limit) {
        if (afterRevision < 0 || limit < 1 || limit > MAX_HISTORY_PAGE) {
            throw new Problem(422, "invalid_release_history_page");
        }
    }

    /** 兼容配置读取入口；新格式返回经过发布与基础清单双重校验后的配置投影。 */
    public ReleaseDefinition require(String hash) {
        return resolve(hash).definition();
    }

    public ResolvedAgentRelease resolve(String hash) {
        requireHash(hash);
        var legacy = repository.findManifest(hash);
        if (legacy != null) {
            verifyLegacy(hash, legacy);
            return new ResolvedAgentRelease(
                    AgentDraft.BUILTIN_AGENT, AgentDraft.BUILTIN_PROMPT, legacy, null, null);
        }
        var manifest = repository.findAgentManifest(hash);
        if (manifest == null || !AgentReleaseIdentity.hash(manifest).equals(hash)) {
            throw new Blocked("release_unavailable");
        }
        manifest.validate();
        var base = requireLegacy(manifest.baseReleaseHash());
        return new ResolvedAgentRelease(
                manifest.agentId(),
                manifest.systemPrompt(),
                base,
                manifest.tenant(),
                manifest.subject());
    }

    private ReleaseDefinition requireLegacy(String hash) {
        requireHash(hash);
        var release = repository.findManifest(hash);
        verifyLegacy(hash, release);
        return release;
    }

    private static void requireHash(String hash) {
        if (hash == null || hash.isBlank()) {
            throw new Blocked("release_unavailable");
        }
    }

    private static void verifyLegacy(String hash, ReleaseDefinition release) {
        if (release == null || !ReleaseIdentity.hash(release).equals(hash)) {
            throw new Blocked("release_unavailable");
        }
    }
}
