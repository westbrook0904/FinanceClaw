package com.financeclaw.infrastructure.memory;

import com.financeclaw.app.memory.port.MemoryRepository;
import com.financeclaw.domain.Memory.*;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.memory.EmbeddingVector;
import java.util.List;
import org.springframework.stereotype.Component;

@Component
public class MyBatisMemoryRepository implements MemoryRepository {
  private final MemoryMapper db;

  public MyBatisMemoryRepository(MemoryMapper db) {
    this.db = db;
  }

  public void ensure(Owner owner) {
    db.ensure(owner);
  }

  public Privacy lock(Owner owner) {
    return db.lock(owner);
  }

  public void source(Source source) {
    db.source(source);
  }

  public Source sourceById(String id) {
    return db.sourceById(id);
  }

  public List<String> currentSources(Owner owner, long epoch, List<String> ids) {
    if (ids.isEmpty()) return List.of();
    return db.currentSources(owner, epoch, ids);
  }

  public int ready(String id) {
    return db.ready(id);
  }

  public void extractJob(String id) {
    db.extractJob(id);
  }

  public void freeze(String turn, Owner owner, long epoch) {
    db.freeze(turn, owner, epoch);
  }

  public List<Fact> recall(String turn, Owner owner) {
    return db.recall(turn, owner);
  }

  public List<Fact> list(Owner owner) {
    return db.list(owner);
  }

  public Fact fact(String id) {
    return db.fact(id);
  }

  public boolean newerActive(Fact fact) {
    return db.newerActive(fact);
  }

  public List<Fact> active(Owner owner) {
    return db.active(owner);
  }

  public int insertFact(Fact fact) {
    return db.insertFact(fact);
  }

  public void supersede(Fact fact) {
    db.supersede(fact);
  }

  public void status(String id, String status) {
    db.status(id, status);
  }

  public void indexJob(String id, String model) {
    db.indexJob(id, model);
  }

  public Job next() {
    return db.next();
  }

  public void claim(long id, String token) {
    db.claim(id, token);
  }

  public Job job(long id) {
    return db.job(id);
  }

  public boolean liveLease(Job job) {
    return db.liveLease(job);
  }

  public void finish(Job job, String status, String error) {
    db.finish(job, status, error);
  }

  public void extracted(String id) {
    db.extracted(id);
  }

  public void project(Fact fact, String model, EmbeddingVector vector) {
    db.project(fact, model, MemoryVectorCodec.array(vector));
  }

  public List<Fact> search(Owner owner, String model, EmbeddingVector vector) {
    return db.search(owner, model, MemoryVectorCodec.query(vector));
  }

  public Operation operation(Owner owner, String key) {
    return db.operation(owner, key);
  }

  public void recordOperation(Owner owner, String key, String hash, String result) {
    db.recordOperation(owner, key, hash, result);
  }

  public void audit(Owner owner, long epoch, String action, String target) {
    db.audit(owner, epoch, action, target);
  }

  public void forget(Owner owner) {
    db.forget(owner);
  }

  public void eraseSources(Owner owner) {
    db.eraseSources(owner);
  }

  public void eraseFacts(Owner owner) {
    db.eraseFacts(owner);
  }

  public void eraseIndex(Owner owner) {
    db.eraseIndex(owner);
  }

  public void eraseRecalls(Owner owner) {
    db.eraseRecalls(owner);
  }

  public void eraseJobs(Owner owner) {
    db.eraseJobs(owner);
  }

  public Privacy purge() {
    return db.purge();
  }

  public int purged(Owner owner) {
    return db.purged(owner);
  }
}
