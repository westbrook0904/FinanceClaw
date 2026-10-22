package com.financeclaw.app.memory.port;

import com.financeclaw.domain.Memory.*;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.memory.EmbeddingVector;
import java.util.List;

/** Memory facts, privacy and durable jobs; implementations join the caller's Spring transaction. */
public interface MemoryRepository {
  void ensure(Owner owner);

  Privacy lock(Owner owner);

  void source(Source source);

  Source sourceById(String id);

  List<String> currentSources(Owner owner, long epoch, List<String> ids);

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

  Job next();

  void claim(long id, String token);

  Job job(long id);

  boolean liveLease(Job job);

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
