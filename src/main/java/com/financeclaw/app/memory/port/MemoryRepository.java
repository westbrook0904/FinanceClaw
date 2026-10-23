package com.financeclaw.app.memory.port;

import com.financeclaw.domain.Memory.*;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.memory.EmbeddingVector;
import com.financeclaw.domain.memory.UserProfile;
import java.util.List;

/** SQL 权威记忆存储边界：源消息、事实、画像、隐私屏障和任务租约加入调用方事务；向量索引可重建。 */
public interface MemoryRepository {
    void ensure(Owner owner);

    Privacy lock(Owner owner);

    /** 受理之间共享读取锁，同时与画像更新和遗忘的排他写入互斥。 */
    Privacy lockForAdmission(Owner owner);

    UserProfile userProfile(Owner owner);

    int advanceProfile(Owner owner, long epoch, long revision);

    void saveUserProfile(Owner owner, UserProfile profile);

    void eraseProfile(Owner owner);

    void source(Source source);

    Source sourceById(String id);

    List<String> currentSources(Owner owner, long epoch, long profileRevision, List<String> ids);

    int ready(String id);

    void extractJob(String id);

    void freeze(String turn, Owner owner, long epoch);

    List<Fact> recall(String turn, Owner owner);

    List<Fact> list(Owner owner);

    Fact fact(String id);

    boolean newerActive(Fact fact);

    List<Fact> active(Owner owner);

    int insertFact(Fact fact);

    void supersede(Fact fact);

    void status(String id, String status);

    void indexJob(String id, String model);

    Job claimJob(String token);

    Job job(long id);

    boolean hasLiveJobLease(Job job);

    void finish(Job job, String status, String error);

    void extracted(String id);

    void project(Fact fact, String model, EmbeddingVector vector);

    List<Fact> search(Owner owner, String model, EmbeddingVector vector);

    Operation operation(Owner owner, String key);

    void recordOperation(Owner owner, String key, String hash, String result);

    void audit(Owner owner, long epoch, String action, String target);

    void forget(Owner owner);

    void eraseSources(Owner owner);

    void eraseFacts(Owner owner);

    void eraseIndex(Owner owner);

    void eraseRecalls(Owner owner);

    void eraseJobs(Owner owner);

    Privacy purge();

    int purged(Owner owner);
}
