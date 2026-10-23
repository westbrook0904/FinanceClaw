package com.financeclaw.app.release;

import com.financeclaw.app.release.port.ReleaseRepository;
import com.financeclaw.app.release.port.ReleaseSource;
import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.Task.Problem;
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
        require(deployment.releaseHash());
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
        require(hash);
        return hash;
    }

    /** 显式发布的兼容入口；并发切换必须报告冲突，不重读新 revision 后静默覆盖。 */
    public void publish(ReleaseDefinition release) {
        String hash = register(release);
        activate(hash, current().revision());
    }

    /** CAS 切换指针并记录不可变历史；回滚到旧摘要也递增 revision，已有 Turn 不受影响。 */
    public Deployment activate(String hash, long expectedRevision) {
        if (expectedRevision < 1) {
            throw new Problem(422, "invalid_release_revision");
        }
        return tx.execute(
                status -> {
                    require(hash);
                    var previous = repository.current(DEFAULT_DEPLOYMENT);
                    if (previous == null
                            || previous.revision() != expectedRevision
                            || !repository.updateDeployment(
                                    DEFAULT_DEPLOYMENT, hash, expectedRevision)) {
                        throw new Problem(409, "release_revision_changed");
                    }
                    var next = new Deployment(DEFAULT_DEPLOYMENT, hash, expectedRevision + 1);
                    repository.insertChange(
                            new DeploymentChange(
                                    DEFAULT_DEPLOYMENT,
                                    next.revision(),
                                    hash,
                                    previous.releaseHash()));
                    return next;
                });
    }

    public List<DeploymentChange> history(long afterRevision, int limit) {
        if (afterRevision < 0 || limit < 1 || limit > MAX_HISTORY_PAGE) {
            throw new Problem(422, "invalid_release_history_page");
        }
        return List.copyOf(repository.history(DEFAULT_DEPLOYMENT, afterRevision, limit));
    }

    public ReleaseDefinition require(String hash) {
        if (hash == null || hash.isBlank()) {
            throw new Blocked("release_unavailable");
        }
        var release = repository.findManifest(hash);
        if (release == null || !ReleaseIdentity.hash(release).equals(hash)) {
            throw new Blocked("release_unavailable");
        }
        return release;
    }
}
