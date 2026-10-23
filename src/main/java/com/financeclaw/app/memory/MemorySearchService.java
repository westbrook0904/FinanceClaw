package com.financeclaw.app.memory;

import com.financeclaw.app.memory.port.*;
import com.financeclaw.domain.Memory.Fact;
import com.financeclaw.domain.Task.*;
import com.financeclaw.domain.memory.*;
import java.util.List;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionTemplate;

/** 证据向量检索入口：向量只负责候选召回，返回前再次验证权威事实、用户归属和当前隐私 epoch。 */
@Service
public class MemorySearchService {
    private final MemoryRepository db;
    private final MemoryEmbedding models;
    private final TransactionTemplate tx;
    private final MemoryPolicy settings;
    private final String revision;

    public MemorySearchService(
            MemoryRepository db,
            MemoryEmbedding models,
            TransactionTemplate tx,
            MemoryPolicy settings) {
        this.db = db;
        this.models = models;
        this.tx = tx;
        this.settings = settings;
        this.revision = settings.embeddingRevision();
    }

    public List<Fact> search(Owner owner, String query) {
        settings.requireSearch(query);
        if (org.springframework.transaction.support.TransactionSynchronizationManager
                .isActualTransactionActive()) {
            throw new IllegalStateException("Memory model work requires no ambient transaction");
        }
        long epoch =
                tx.execute(
                        s -> {
                            db.ensure(owner);
                            return db.lock(owner).epoch();
                        });
        EmbeddingVector vector;
        try {
            vector = models.embed(query);
        } catch (RuntimeException e) {
            throw new Problem(503, "embedding_unavailable");
        }
        return tx.execute(
                s -> {
                    var p = db.lock(owner);
                    if (!p.permits(epoch)) {
                        return List.of();
                    }
                    return db.search(owner, revision, vector);
                });
    }
}
