package com.financeclaw.app.memory;

import com.financeclaw.app.memory.port.*;
import com.financeclaw.domain.Memory.*;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.memory.EmbeddingVector;
import java.util.*;
import org.springframework.transaction.support.TransactionTemplate;

/** Network work happens outside transactions. Every result rechecks lease, epoch and revision. */
public final class MemoryProcessingService {
  private final MemoryRepository db;
  private final TransactionTemplate tx;
  private final MemoryExtraction extractor;
  private final MemoryEmbedding embeddings;
  private final String modelRevision;

  public MemoryProcessingService(
      MemoryRepository db,
      TransactionTemplate tx,
      MemoryExtraction extractor,
      MemoryEmbedding embeddings,
      String modelRevision) {
    this.db = db;
    this.tx = tx;
    this.extractor = extractor;
    this.embeddings = embeddings;
    this.modelRevision = modelRevision;
  }

  public Job claim() {
    return tx.execute(
        s -> {
          var job = db.next();
          if (job == null) return null;
          db.claim(job.id(), UUID.randomUUID().toString());
          return db.job(job.id());
        });
  }

  private boolean live(Job job, Privacy p) {
    var now = db.job(job.id());
    return job.live(p, now, db.liveLease(job));
  }

  public boolean poll() {
    boolean purged =
        tx.execute(
            s -> {
              var p = db.purge();
              if (p == null) return false;
              var o = new Owner(p.tenant(), p.subject());
              db.purged(o);
              db.audit(o, p.epoch(), "purge_complete", "all");
              return true;
            });
    var job = claim();
    if (job == null) return purged;
    process(job);
    return true;
  }

  public void process(Job job) {
    if (org.springframework.transaction.support.TransactionSynchronizationManager
        .isActualTransactionActive())
      throw new IllegalStateException("Memory model work requires no ambient transaction");
    Owner owner = new Owner(job.tenant(), job.subject());
    try {
      boolean valid = tx.execute(s -> live(job, db.lock(owner)));
      if (!valid) {
        tx.executeWithoutResult(s -> db.finish(job, "obsolete", null));
        return;
      }
      if (job.kind().equals("extract")) {
        var source = db.sourceById(job.keyId());
        if (source == null || !source.extractable(job))
          throw new IllegalStateException("source_unavailable");
        var result = extractor.extract(source.content());
        commitExtraction(job, source, result);
      } else {
        var fact = db.fact(job.keyId());
        if (fact == null
            || !fact.activeAt(job.epoch(), job.revision())
            || !modelRevision.equals(job.modelRevision())) {
          tx.executeWithoutResult(s -> db.finish(job, "obsolete", null));
          return;
        }
        EmbeddingVector vector = embeddings.embed(fact.key() + ": " + fact.content());
        tx.executeWithoutResult(
            s -> {
              var p = db.lock(owner);
              if (!live(job, p)) return;
              var current = db.fact(fact.id());
              if (current == null || !current.activeAt(job.epoch(), job.revision())) {
                db.finish(job, "obsolete", null);
                return;
              }
              db.project(current, modelRevision, vector);
              db.finish(job, "done", null);
            });
      }
    } catch (RuntimeException e) {
      // Persist a bounded code, never model/source/transport exception text.
      tx.executeWithoutResult(
          s ->
              db.finish(
                  job,
                  "pending",
                  job.kind().equals("extract") ? "extraction_failed" : "index_failed"));
    }
  }

  public void commitExtraction(Job job, Source source, Extraction result) {
    if (result == null) throw new IllegalArgumentException("invalid_extraction");
    result.validateAgainst(source);
    Owner owner = new Owner(job.tenant(), job.subject());
    tx.executeWithoutResult(
        s -> {
          var p = db.lock(owner);
          if (!live(job, p)) return;
          var current = db.sourceById(source.id());
          if (current == null || !current.pendingAt(p.epoch())) return;
          for (var proposal : result.proposals()) {
            boolean auto = proposal.equals(Proposal.explicit(current.content()));
            var fact =
                new Fact(
                    UUID.randomUUID().toString(),
                    source.id(),
                    owner.tenant(),
                    owner.subject(),
                    p.epoch(),
                    proposal.key(),
                    proposal.value(),
                    proposal.quote(),
                    1,
                    auto ? "active" : "candidate");
            if (auto && db.newerActive(fact)) {
              db.insertFact(
                  new Fact(
                      fact.id(),
                      fact.sourceId(),
                      fact.tenant(),
                      fact.subject(),
                      fact.epoch(),
                      fact.key(),
                      fact.content(),
                      fact.evidence(),
                      1,
                      "superseded"));
              db.audit(owner, p.epoch(), "superseded_extraction", fact.id());
              continue;
            }
            if (auto) db.supersede(fact);
            if (db.insertFact(fact) == 1) {
              if (auto) db.indexJob(fact.id(), modelRevision);
              db.audit(owner, p.epoch(), auto ? "explicit_preference" : "candidate", fact.id());
            }
          }
          db.extracted(source.id());
          db.finish(job, "done", null);
        });
  }
}
