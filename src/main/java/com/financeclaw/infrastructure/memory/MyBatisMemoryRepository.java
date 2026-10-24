package com.financeclaw.infrastructure.memory;

import com.financeclaw.app.memory.port.MemoryRepository;
import com.financeclaw.domain.Memory.*;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.memory.EmbeddingVector;
import com.financeclaw.domain.memory.TurnMemorySnapshot;
import com.financeclaw.domain.memory.UserProfile;
import java.util.List;
import org.springframework.stereotype.Component;

/**
 * 实现 MemoryRepository，把画像、证据和作业端口映射到 MemoryMapper。
 * 受理共享锁与修改排他锁分别委托，事务由调用用例持有；空批量来源查询直接返回空列表。向量对象在此编码为数据库数组文本，不将可重建索引提升为记忆权威。
 */
@Component
public class MyBatisMemoryRepository implements MemoryRepository {
    private final MemoryMapper db;

    public MyBatisMemoryRepository(MemoryMapper db) {
        this.db = db;
    }

    @Override
    public void ensure(Owner owner) {
        db.insertOwnerIfAbsent(owner);
    }

    @Override
    public Privacy lock(Owner owner) {
        return db.getOwnerPrivacyForUpdate(owner);
    }

    @Override
    public Privacy lockForAdmission(Owner owner) {
        return db.getOwnerPrivacyForShare(owner);
    }

    @Override
    public UserProfile userProfile(Owner owner) {
        return db.getUserProfile(owner);
    }

    @Override
    public int advanceProfile(Owner owner, long epoch, long revision) {
        return db.updateProfileRevisionByEpochAndExpectedRevision(owner, epoch, revision);
    }

    @Override
    public void saveUserProfile(Owner owner, UserProfile profile) {
        db.insertOrUpdateUserProfile(owner, profile);
    }

    @Override
    public void eraseProfile(Owner owner) {
        db.deleteUserProfile(owner);
    }

    @Override
    public void source(Source source) {
        db.insertSource(source);
    }

    @Override
    public Source sourceById(String id) {
        return db.getSourceById(id);
    }

    @Override
    public List<String> currentSources(Owner owner, long epoch, List<String> ids) {
        if (ids.isEmpty()) {
            return List.of();
        }
        return db.listCurrentSourceIds(owner, epoch, ids);
    }

    @Override
    public int ready(String id) {
        return db.updateAdmittedSourcePending(id);
    }

    @Override
    public void extractJob(String id) {
        db.insertExtractJobIfAbsent(id);
    }

    @Override
    public List<Fact> snapshotFacts(Owner owner, long epoch) {
        return db.listFactsForTurnSnapshot(owner, epoch);
    }

    @Override
    public void insertTurnSnapshot(TurnMemorySnapshot snapshot) {
        db.insertTurnMemorySnapshot(snapshot);
    }

    @Override
    public TurnMemorySnapshot turnSnapshot(String turn, Owner owner) {
        return db.getTurnMemorySnapshot(turn, owner);
    }

    @Override
    public List<Fact> list(Owner owner) {
        return db.listVisibleFacts(owner);
    }

    @Override
    public Fact fact(String id) {
        return db.getFactById(id);
    }

    @Override
    public boolean newerActive(Fact fact) {
        return db.existsNewerActiveFact(fact);
    }

    @Override
    public List<Fact> active(Owner owner) {
        return db.listActiveFacts(owner);
    }

    @Override
    public int insertFact(Fact fact) {
        return db.insertFactIfAbsent(fact);
    }

    @Override
    public void supersede(Fact fact) {
        db.updateActiveFactsSuperseded(fact);
    }

    @Override
    public void status(String id, String status) {
        db.updateFactStatus(id, status);
    }

    @Override
    public void indexJob(String id, String model) {
        db.insertOrRequeueIndexJob(id, model);
    }

    @Override
    public Job claimJob(String token) {
        return db.updateNextJobLease(token);
    }

    @Override
    public Job job(long id) {
        return db.getJobForUpdate(id);
    }

    @Override
    public boolean hasLiveJobLease(Job job) {
        return db.getJobLeaseLiveForUpdate(job);
    }

    @Override
    public void finish(Job job, String status, String error) {
        db.updateRunningJobOutcomeByLeaseToken(job, status, error);
    }

    @Override
    public void extracted(String id) {
        db.updatePendingSourceExtracted(id);
    }

    @Override
    public void project(Fact fact, String model, EmbeddingVector vector) {
        db.insertOrUpdateFactProjection(fact, model, MemoryVectorCodec.array(vector));
    }

    @Override
    public List<Fact> search(Owner owner, String model, EmbeddingVector vector) {
        return db.listSimilarActiveFacts(owner, model, MemoryVectorCodec.query(vector));
    }

    @Override
    public Operation operation(Owner owner, String key) {
        return db.getOperationByIdempotencyKey(owner, key);
    }

    @Override
    public void recordOperation(Owner owner, String key, String hash, String result) {
        db.insertOperationReceipt(owner, key, hash, result);
    }

    @Override
    public void audit(Owner owner, long epoch, String action, String target) {
        db.insertAuditEntry(owner, epoch, action, target);
    }

    @Override
    public void forget(Owner owner) {
        db.updateOwnerPrivacyForForget(owner);
    }

    @Override
    public void eraseSources(Owner owner) {
        db.updateSourcesForgotten(owner);
    }

    @Override
    public void eraseFacts(Owner owner) {
        db.updateFactsForgotten(owner);
    }

    @Override
    public void eraseIndex(Owner owner) {
        db.deleteFactProjections(owner);
    }

    @Override
    public void eraseSnapshots(Owner owner) {
        db.deleteTurnSnapshotsByOwner(owner);
    }

    @Override
    public void eraseWorkContexts(Owner owner) {
        db.deleteWorkContextsByOwner(owner);
    }

    @Override
    public void eraseExecutionData(Owner owner, long beforeEpoch) {
        db.deleteExecutionDataBeforeEpoch(owner, beforeEpoch);
    }

    @Override
    public void eraseJobs(Owner owner) {
        db.updateJobsForgotten(owner);
    }

    @Override
    public Privacy purge() {
        return db.getNextPurgeOwnerForUpdateSkipLocked();
    }

    @Override
    public int purged(Owner owner) {
        return db.updateOwnerPurgeCompleted(owner);
    }
}
