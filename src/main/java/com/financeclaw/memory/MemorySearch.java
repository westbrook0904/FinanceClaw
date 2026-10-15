package com.financeclaw.memory;

import com.financeclaw.domain.Memory.Fact;
import com.financeclaw.domain.Task.*;
import com.financeclaw.infrastructure.memory.MemoryMapper;
import java.time.Duration;
import java.util.List;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionTemplate;

@Service
public class MemorySearch {
  private final MemoryMapper db;
  private final MemoryModels models;
  private final TransactionTemplate tx;
  private final com.financeclaw.application.MemoryService memory;
  private final boolean enabled;
  private final String revision;

  public MemorySearch(
      MemoryMapper db,
      MemoryModels models,
      com.financeclaw.application.MemoryService memory,
      TransactionTemplate tx,
      @Value("${financeclaw.memory.vector-search:false}") boolean enabled,
      @Value("${financeclaw.memory.embedding.revision:disabled}") String revision) {
    this.db = db;
    this.memory = memory;
    this.models = models;
    this.tx = tx;
    this.enabled = enabled;
    this.revision = revision;
  }

  public List<Fact> search(Owner owner, String query) {
    if (!enabled || !memory.enabled()) throw new Problem(503, "vector_search_disabled");
    if (query == null || query.isBlank() || query.length() > 1024)
      throw new Problem(422, "invalid_query");
    long epoch =
        tx.execute(
            s -> {
              db.ensure(owner);
              return db.lock(owner).epoch();
            });
    String vector;
    try {
      var model = models.embedding();
      vector =
          MemoryProcessor.vector(
              model
                  .embed(io.agentscope.core.message.TextBlock.builder().text(query).build())
                  .subscribeOn(reactor.core.scheduler.Schedulers.boundedElastic())
                  .block(Duration.ofSeconds(20)));
    } catch (RuntimeException e) {
      throw new Problem(503, "embedding_unavailable");
    }
    String vectorLiteral = vector.replace('{', '[').replace('}', ']');
    return tx.execute(
        s -> {
          var p = db.lock(owner);
          if (p.epoch() != epoch || p.purgePending()) return List.of();
          return db.search(owner, revision, vectorLiteral);
        });
  }
}
