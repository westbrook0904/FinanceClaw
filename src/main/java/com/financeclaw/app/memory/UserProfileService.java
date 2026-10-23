package com.financeclaw.app.memory;

import com.financeclaw.app.memory.port.MemoryRepository;
import com.financeclaw.app.task.TaskEventHub;
import com.financeclaw.domain.Memory.Privacy;
import com.financeclaw.domain.Task.*;
import com.financeclaw.domain.memory.MemoryPolicy;
import com.financeclaw.domain.memory.UserProfile;
import org.springframework.transaction.support.TransactionTemplate;

/** 人工 Markdown 画像管理：按用户隔离并通过 epoch 与 revision 拒绝过期编辑；只有事务提交后的版本变更才使旧执行尝试失效。 */
public final class UserProfileService {
    private final MemoryRepository db;
    private final TransactionTemplate tx;
    private final MemoryPolicy settings;
    private final TaskEventHub events;

    public UserProfileService(
            MemoryRepository db,
            TransactionTemplate tx,
            MemoryPolicy settings,
            TaskEventHub events) {
        this.db = db;
        this.tx = tx;
        this.settings = settings;
        this.events = events;
    }

    /** 读取当前用户画像；空画像使用版本零，任何正文与权威版本不一致都拒绝静默降级。 */
    public UserProfile read(Owner owner) {
        settings.requireProfileEnabled();
        return tx.execute(s -> readCurrent(db, owner, lock(owner)));
    }

    /** 通过 epoch 和 revision 比较后更新人工画像；正文不变时不制造新版本，修改与隐私检查共享同一用户排他锁。 */
    public UserProfile update(
            Owner owner, long expectedEpoch, long expectedRevision, String markdown) {
        settings.requireProfileEnabled();
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
                    // 仍持有用户锁时捕获受影响尝试；只有事务提交后才发布失效事件，回滚不影响现有执行。
                    events.invalidateOwner(owner);
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
