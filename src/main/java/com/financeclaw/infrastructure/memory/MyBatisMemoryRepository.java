package com.financeclaw.infrastructure.memory;

import com.financeclaw.app.memory.port.MemoryRepository;
import com.financeclaw.domain.Memory.*;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.memory.EmbeddingVector;
import com.financeclaw.domain.memory.UserProfile;
import java.util.List;
import org.springframework.stereotype.Component;

@Component
public class MyBatisMemoryRepository implements MemoryRepository {
  private final MemoryMapper db;

  public MyBatisMemoryRepository(MemoryMapper db) {
    this.db = db;
  }

  public void ensure(Owner owner) {
    db.insertOwnerIfAbsent(owner);
  }

  public Privacy lock(Owner owner) {
    return db.getOwnerPrivacyForUpdate(owner);
  }

  public Privacy lockForAdmission(Owner owner) {
    return db.getOwnerPrivacyForShare(owner);
  }

  public UserProfile userProfile(Owner owner) {
    return db.getUserProfile(owner);
  }

  public int advanceProfile(Owner owner, long epoch, long revision) {
    return db.updateProfileRevisionByEpochAndExpectedRevision(owner, epoch, revision);
  }

  public void saveUserProfile(Owner owner, UserProfile profile) {
    db.insertOrUpdateUserProfile(owner, profile);
  }

  public void eraseProfile(Owner owner) {
    db.deleteUserProfile(owner);
  }

  public void source(Source source) {
    db.insertSource(source);
  }

  public Source sourceById(String id) {
    return db.getSourceById(id);
  }

  public List<String> currentSources(
      Owner owner, long epoch, long profileRevision, List<String> ids) {
    if (ids.isEmpty()) return List.of();
    return db.listCurrentSourceIds(owner, epoch, profileRevision, ids);
  }

  public int ready(String id) {
    return db.updateAdmittedSourcePending(id);
  }

  public void extractJob(String id) {
    db.insertExtractJobIfAbsent(id);
  }

  public void freeze(String turn, Owner owner, long epoch) {
    db.insertFrozenRecallFacts(turn, owner, epoch);
  }

  public List<Fact> recall(String turn, Owner owner) {
    return db.listFrozenRecallFacts(turn, owner);
  }

  public List<Fact> list(Owner owner) {
    return db.listVisibleFacts(owner);
  }

  public Fact fact(String id) {
    return db.getFactById(id);
  }

  public boolean newerActive(Fact fact) {
    return db.existsNewerActiveFact(fact);
  }

  public List<Fact> active(Owner owner) {
    return db.listActiveFacts(owner);
  }

  public int insertFact(Fact fact) {
    return db.insertFactIfAbsent(fact);
  }

  public void supersede(Fact fact) {
    db.updateActiveFactsSuperseded(fact);
  }

  public void status(String id, String status) {
    db.updateFactStatus(id, status);
  }

  public void indexJob(String id, String model) {
    db.insertOrRequeueIndexJob(id, model);
  }

  public Job claimJob(String token) {
    return db.updateNextJobLease(token);
  }

  public Job job(long id) {
    return db.getJobForUpdate(id);
  }

  public boolean hasLiveJobLease(Job job) {
    return db.getJobLeaseLiveForUpdate(job);
  }

  public void finish(Job job, String status, String error) {
    db.updateRunningJobOutcomeByLeaseToken(job, status, error);
  }

  public void extracted(String id) {
    db.updatePendingSourceExtracted(id);
  }

  public void project(Fact fact, String model, EmbeddingVector vector) {
    db.insertOrUpdateFactProjection(fact, model, MemoryVectorCodec.array(vector));
  }

  public List<Fact> search(Owner owner, String model, EmbeddingVector vector) {
    return db.listSimilarActiveFacts(owner, model, MemoryVectorCodec.query(vector));
  }

  public Operation operation(Owner owner, String key) {
    return db.getOperationByIdempotencyKey(owner, key);
  }

  public void recordOperation(Owner owner, String key, String hash, String result) {
    db.insertOperationReceipt(owner, key, hash, result);
  }

  public void audit(Owner owner, long epoch, String action, String target) {
    db.insertAuditEntry(owner, epoch, action, target);
  }

  public void forget(Owner owner) {
    db.updateOwnerPrivacyForForget(owner);
  }

  public void eraseSources(Owner owner) {
    db.updateSourcesForgotten(owner);
  }

  public void eraseFacts(Owner owner) {
    db.updateFactsForgotten(owner);
  }

  public void eraseIndex(Owner owner) {
    db.deleteFactProjections(owner);
  }

  public void eraseRecalls(Owner owner) {
    db.deleteFrozenRecalls(owner);
  }

  public void eraseJobs(Owner owner) {
    db.updateJobsForgotten(owner);
  }

  public Privacy purge() {
    return db.getNextPurgeOwnerForUpdateSkipLocked();
  }

  public int purged(Owner owner) {
    return db.updateOwnerPurgeCompleted(owner);
  }
}
