package com.financeclaw.app.memory;

import com.financeclaw.app.memory.port.MemoryRepository;
import com.financeclaw.app.memory.port.ProfileDocuments;
import com.financeclaw.domain.Memory.Privacy;
import com.financeclaw.domain.Task.*;
import com.financeclaw.domain.memory.MemoryPolicy;
import com.financeclaw.domain.memory.UserProfile;
import org.springframework.transaction.support.TransactionTemplate;

/**
 * 人工 Markdown 画像管理：目录模式以服务器文件为人工入口，SQL 保存带 epoch/revision 的采样镜像； HTTP
 * 模式通过版本前置条件更新正文。两种入口的修改都只影响之后受理的 Turn，既有执行沿用冻结正文。
 */
public final class UserProfileService {
    private final MemoryRepository db;
    private final TransactionTemplate tx;
    private final MemoryPolicy settings;
    private final ProfileDocuments documents;

    public UserProfileService(MemoryRepository db, TransactionTemplate tx, MemoryPolicy settings) {
        this(db, tx, settings, ProfileDocuments.disabled());
    }

    public UserProfileService(
            MemoryRepository db,
            TransactionTemplate tx,
            MemoryPolicy settings,
            ProfileDocuments documents) {
        this.db = db;
        this.tx = tx;
        this.settings = settings;
        this.documents = documents;
    }

    /** 读取当前用户画像；空画像使用版本零，任何正文与权威版本不一致都拒绝静默降级。 */
    public UserProfile read(Owner owner) {
        settings.requireProfileEnabled();
        return tx.execute(s -> synchronizedProfile(owner).profile());
    }

    public boolean directoryManaged() {
        return documents.enabled();
    }

    public record Snapshot(Privacy privacy, UserProfile profile) {}

    /** 调用方必须持有受理事务；先获取排他锁再读文件，避免共享锁升级造成同用户并发死锁。 */
    public Snapshot freeze(Owner owner) {
        settings.requireProfileEnabled();
        return synchronizedProfile(owner);
    }

    /** 在同一个用户锁内比较完整正文；内容相同时不写版本、画像或审计，不按 mtime 制造新版本。 */
    private Snapshot synchronizedProfile(Owner owner) {
        var privacy = lock(owner);
        var current = readCurrent(db, owner, privacy);
        if (!documents.enabled()) {
            return new Snapshot(privacy, current);
        }
        String markdown = documents.read(owner, privacy.epoch());
        UserProfile.validate(privacy.epoch(), privacy.profileRevision(), markdown);
        if (current.markdown().equals(markdown)) {
            return new Snapshot(privacy, current);
        }
        if (db.advanceProfile(owner, privacy.epoch(), privacy.profileRevision()) != 1) {
            throw new Problem(409, "profile_revision_conflict");
        }
        var next = new UserProfile(privacy.epoch(), privacy.profileRevision() + 1, markdown);
        db.saveUserProfile(owner, next);
        db.audit(owner, privacy.epoch(), "profile_file_update", "profile.md");
        return new Snapshot(
                new Privacy(
                        privacy.tenant(),
                        privacy.subject(),
                        privacy.epoch(),
                        privacy.purgePending(),
                        next.revision()),
                next);
    }

    /** 通过 epoch 和 revision 比较后更新人工画像；正文不变时不制造新版本，修改与隐私检查共享同一用户排他锁。 */
    public UserProfile update(
            Owner owner, long expectedEpoch, long expectedRevision, String markdown) {
        settings.requireProfileEnabled();
        // 目录模式只接受服务器人工编辑，避免 HTTP 和文件双向覆盖造成更新丢失。
        if (documents.enabled()) {
            throw new Problem(409, "profile_managed_on_server");
        }
        UserProfile.validate(expectedEpoch, expectedRevision, markdown);
        return tx.execute(
                s -> {
                    // 用户行排他锁与受理共享锁、遗忘排他锁互斥，保证版本绑定和正文更新的顺序。
                    var privacy = lock(owner);
                    privacy.requireAvailable();
                    privacy.requireEpoch(expectedEpoch);
                    if (privacy.profileRevision() != expectedRevision) {
                        throw new Problem(409, "profile_revision_conflict");
                    }
                    var current = readCurrent(db, owner, privacy);
                    if (current.markdown().equals(markdown)) {
                        return current;
                    }
                    if (db.advanceProfile(owner, expectedEpoch, expectedRevision) != 1) {
                        throw new Problem(409, "profile_revision_conflict");
                    }
                    var updated = new UserProfile(expectedEpoch, expectedRevision + 1, markdown);
                    db.saveUserProfile(owner, updated);
                    db.audit(owner, expectedEpoch, "profile_update", "profile.md");
                    // 只影响后续受理的新 Turn；已有长期记忆快照在遗忘前保持不变。
                    return updated;
                });
    }

    private Privacy lock(Owner owner) {
        var existing = db.lock(owner);
        if (existing != null) {
            return existing;
        }
        db.ensure(owner);
        return db.lock(owner);
    }

    static UserProfile readCurrent(MemoryRepository db, Owner owner, Privacy privacy) {
        privacy.requireAvailable();
        if (privacy.profileRevision() == 0) {
            return new UserProfile(privacy.epoch(), 0, "");
        }
        var profile = db.userProfile(owner);
        if (profile == null
                || profile.epoch() != privacy.epoch()
                || profile.revision() != privacy.profileRevision()) {
            throw new Problem(503, "profile_unavailable");
        }
        return profile;
    }
}
