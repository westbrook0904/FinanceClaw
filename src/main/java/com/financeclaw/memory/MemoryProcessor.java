package com.financeclaw.memory;

import com.financeclaw.domain.Memory.*;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.infrastructure.memory.MemoryMapper;
import io.agentscope.core.embedding.EmbeddingModel;
import io.agentscope.core.message.TextBlock;
import java.time.Duration;
import java.util.*;
import java.util.function.Function;
import java.util.function.Supplier;
import org.springframework.transaction.support.TransactionTemplate;

/** Network work happens outside transactions. Every result rechecks lease, epoch and revision. */
public final class MemoryProcessor {
  private final MemoryMapper db;
  private final TransactionTemplate tx;
  private final Function<String, Extraction> extractor;
  private final Supplier<EmbeddingModel> embeddings;
  private final String modelRevision;

  public MemoryProcessor(
      MemoryMapper db,
      TransactionTemplate tx,
      Function<String, Extraction> extractor,
      Supplier<EmbeddingModel> embeddings,
      String modelRevision) {
    this.db = db;
    this.tx = tx;
    this.extractor = extractor;
    this.embeddings = embeddings;
    this.modelRevision = modelRevision;
  }

  public static Proposal explicit(String text) {
    if (text == null) return null;
    for (var entry :
        Map.of("response_language", Set.of("中文", "English"), "response_style", Set.of("简洁", "详细"))
            .entrySet())
      for (var value : entry.getValue())
        if (text.equals("/remember " + entry.getKey() + "=" + value))
          return new Proposal(entry.getKey(), value, text);
    return null;
  }

  public static String vector(double[] vector) {
    if (vector == null || vector.length != 1024)
      throw new IllegalArgumentException("embedding_dimension_mismatch");
    double norm = 0;
    var parts = new ArrayList<String>(1024);
    for (double v : vector) {
      if (!Double.isFinite(v) || Math.abs(v) > Float.MAX_VALUE)
        throw new IllegalArgumentException("invalid_embedding");
      norm += v * v;
      parts.add(Float.toString((float) v));
    }
    if (norm == 0) throw new IllegalArgumentException("zero_embedding");
    return "{" + String.join(",", parts) + "}";
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
    return db.liveLease(job)
        && p != null
        && !p.purgePending()
        && p.epoch() == job.epoch()
        && now != null
        && now.status().equals("running")
        && Objects.equals(now.leaseToken(), job.leaseToken());
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
    Owner owner = new Owner(job.tenant(), job.subject());
    try {
      boolean valid = tx.execute(s -> live(job, db.lock(owner)));
      if (!valid) {
        tx.executeWithoutResult(s -> db.finish(job, "obsolete", null));
        return;
      }
      if (job.kind().equals("extract")) {
        var source = db.sourceById(job.keyId());
        if (source == null
            || source.content() == null
            || !source.status().equals("pending")
            || source.epoch() != job.epoch()) throw new IllegalStateException("source_unavailable");
        var result = extractor.apply(source.content());
        commitExtraction(job, source, result);
      } else {
        var fact = db.fact(job.keyId());
        if (fact == null
            || !fact.status().equals("active")
            || fact.epoch() != job.epoch()
            || fact.revision() != job.revision()
            || !modelRevision.equals(job.modelRevision())) {
          tx.executeWithoutResult(s -> db.finish(job, "obsolete", null));
          return;
        }
        var model = embeddings.get();
        if (model.getDimensions() != 1024)
          throw new IllegalArgumentException("embedding_dimension_mismatch");
        String vector =
            vector(
                model
                    .embed(TextBlock.builder().text(fact.key() + ": " + fact.content()).build())
                    .subscribeOn(reactor.core.scheduler.Schedulers.boundedElastic())
                    .block(Duration.ofSeconds(20)));
        tx.executeWithoutResult(
            s -> {
              var p = db.lock(owner);
              if (!live(job, p)) return;
              var current = db.fact(fact.id());
              if (current == null
                  || !current.status().equals("active")
                  || current.epoch() != job.epoch()
                  || current.revision() != job.revision()) {
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
    if (result == null || result.proposals() == null || result.proposals().size() > 8)
      throw new IllegalArgumentException("invalid_extraction");
    Set<String> keys = new HashSet<>();
    for (var p : result.proposals()) {
      if (p == null
          || !Set.of(
                  "response_language",
                  "response_style",
                  "home_city",
                  "travel_preference",
                  "investment_preference")
              .contains(p.key())
          || !keys.add(p.key())
          || p.value() == null
          || p.value().isBlank()
          || p.value().length() > 256
          || p.quote() == null
          || p.quote().length() > 1024
          || !source.content().contains(p.quote())
          || !p.quote().contains(p.value()))
        throw new IllegalArgumentException("untrusted_extraction");
    }
    Owner owner = new Owner(job.tenant(), job.subject());
    tx.executeWithoutResult(
        s -> {
          var p = db.lock(owner);
          if (!live(job, p)) return;
          var current = db.sourceById(source.id());
          if (current == null
              || current.epoch() != p.epoch()
              || !current.status().equals("pending")) return;
          for (var proposal : result.proposals()) {
            boolean auto = proposal.equals(explicit(current.content()));
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
