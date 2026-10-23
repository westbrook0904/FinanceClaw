package com.financeclaw.app.memory;

import com.financeclaw.app.memory.port.MemoryRepository;
import com.financeclaw.app.task.TaskEventHub;
import com.financeclaw.domain.Memory.Privacy;
import com.financeclaw.domain.Task.*;
import com.financeclaw.domain.memory.MemoryPolicy;
import com.financeclaw.domain.memory.UserProfile;
import org.springframework.transaction.support.TransactionTemplate;

/** Manual profile changes serialize with admission, forgetting, and other profile editors. */
public final class UserProfileService {
  private final MemoryRepository db;
  private final TransactionTemplate tx;
  private final MemoryPolicy settings;
  private final TaskEventHub events;

  public UserProfileService(
      MemoryRepository db, TransactionTemplate tx, MemoryPolicy settings, TaskEventHub events) {
    this.db = db;
    this.tx = tx;
    this.settings = settings;
    this.events = events;
  }

  public UserProfile read(Owner owner) {
    settings.requireProfileEnabled();
    return tx.execute(s -> readCurrent(db, owner, lock(owner)));
  }

  public UserProfile update(
      Owner owner, long expectedEpoch, long expectedRevision, String markdown) {
    settings.requireProfileEnabled();
    UserProfile.validate(expectedEpoch, expectedRevision, markdown);
    return tx.execute(
        s -> {
          var privacy = lock(owner);
          privacy.requireAvailable();
          privacy.requireEpoch(expectedEpoch);
          if (privacy.profileRevision() != expectedRevision)
            throw new Problem(409, "profile_revision_conflict");
          var current = readCurrent(db, owner, privacy);
          if (current.markdown().equals(markdown)) return current;
          if (db.advanceProfile(owner, expectedEpoch, expectedRevision) != 1)
            throw new Problem(409, "profile_revision_conflict");
          var updated = new UserProfile(expectedEpoch, expectedRevision + 1, markdown);
          db.saveUserProfile(owner, updated);
          db.audit(owner, expectedEpoch, "profile_update", "profile.md");
          // Capture affected attempts while holding the owner lock; Hub publishes after commit.
          events.invalidateOwner(owner);
          return updated;
        });
  }

  private Privacy lock(Owner owner) {
    var existing = db.lock(owner);
    if (existing != null) return existing;
    db.ensure(owner);
    return db.lock(owner);
  }

  static UserProfile readCurrent(MemoryRepository db, Owner owner, Privacy privacy) {
    privacy.requireAvailable();
    if (privacy.profileRevision() == 0) return new UserProfile(privacy.epoch(), 0, "");
    var profile = db.userProfile(owner);
    if (profile == null
        || profile.epoch() != privacy.epoch()
        || profile.revision() != privacy.profileRevision())
      throw new Problem(503, "profile_unavailable");
    return profile;
  }
}
