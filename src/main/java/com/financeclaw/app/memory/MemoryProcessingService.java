package com.financeclaw.app.memory;

import com.financeclaw.app.memory.port.*;
import com.financeclaw.domain.Memory.*;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.memory.EmbeddingVector;
import java.util.*;
import org.springframework.transaction.support.TransactionTemplate;

/** 跨会话证据后台处理：领取租约后在事务外提取或嵌入，提交前重新锁定用户并检查 epoch、任务令牌及事实版本。 */
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

    /** 在一个 SQL 中领取待执行或已过期任务并返回新租约；同一候选行通过 SKIP LOCKED 只交给一个 Worker。 */
    public Job claim() {
        return tx.execute(s -> db.claimJob(UUID.randomUUID().toString()));
    }

    private boolean live(Job job, Privacy p) {
        return job.live(p, db.hasLiveJobLease(job));
    }

    public boolean poll() {
        boolean purged =
                tx.execute(
                        s -> {
                            var p = db.purge();
                            if (p == null) {
                                return false;
                            }
                            var o = new Owner(p.tenant(), p.subject());
                            if (db.purged(o) != 1) {
                                return false;
                            }
                            db.audit(o, p.epoch(), "purge_complete", "all");
                            return true;
                        });
        var job = claim();
        if (job == null) {
            return purged;
        }
        process(job);
        return true;
    }

    /** 在主库事务外调用提取或嵌入模型；完成后再次核验用户隐私和任务令牌，过期或被遗忘的结果不得写回。 */
    public void process(Job job) {
        if (org.springframework.transaction.support.TransactionSynchronizationManager
                .isActualTransactionActive()) {
            throw new IllegalStateException("Memory model work requires no ambient transaction");
        }
        Owner owner = new Owner(job.tenant(), job.subject());
        try {
            boolean valid = tx.execute(s -> live(job, db.lock(owner)));
            if (!valid) {
                tx.executeWithoutResult(s -> db.finish(job, "obsolete", null));
                return;
            }
            if (job.kind().equals("extract")) {
                var source = db.sourceById(job.keyId());
                if (source == null || !source.extractable(job)) {
                    throw new IllegalStateException("source_unavailable");
                }
                // 模型调用不持有用户或任务行锁，其他会话受理与遗忘无需等待网络返回。
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
                            // 写回之前先锁用户，再锁任务并使用数据库时间核验租约；令牌变更说明结果已经过期。
                            var p = db.lock(owner);
                            if (!live(job, p)) {
                                return;
                            }
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
                                    job.kind().equals("extract")
                                            ? "extraction_failed"
                                            : "index_failed"));
        }
    }

    public void commitExtraction(Job job, Source source, Extraction result) {
        if (result == null) {
            throw new IllegalArgumentException("invalid_extraction");
        }
        result.validateAgainst(source);
        Owner owner = new Owner(job.tenant(), job.subject());
        tx.executeWithoutResult(
                s -> {
                    // 写回之前先锁用户，再锁任务并使用数据库时间核验租约；令牌变更说明结果已经过期。
                    var p = db.lock(owner);
                    if (!live(job, p)) {
                        return;
                    }
                    var current = db.sourceById(source.id());
                    if (current == null || !current.pendingAt(p.epoch())) {
                        return;
                    }
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
                        if (auto) {
                            db.supersede(fact);
                        }
                        if (db.insertFact(fact) == 1) {
                            if (auto) {
                                db.indexJob(fact.id(), modelRevision);
                            }
                            db.audit(
                                    owner,
                                    p.epoch(),
                                    auto ? "explicit_preference" : "candidate",
                                    fact.id());
                        }
                    }
                    db.extracted(source.id());
                    db.finish(job, "done", null);
                });
    }
}
