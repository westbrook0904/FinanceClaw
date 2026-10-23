package com.financeclaw.app.memory;

import com.financeclaw.app.memory.port.*;
import com.financeclaw.app.task.TaskEventHub;
import com.financeclaw.domain.IdempotencyKey;
import com.financeclaw.domain.Task.*;
import com.financeclaw.domain.memory.ForgetFact;
import java.util.Objects;
import java.util.UUID;
import org.springframework.transaction.support.TransactionSynchronizationManager;
import org.springframework.transaction.support.TransactionTemplate;

/** 遗忘事务编排器：本地模式原子擦除；独立账本模式先持久化阻断屏障，再向账本追加同一事实，最后回主库擦除并记录回执。 */
public final class MemoryForgetService {
    private final MemoryRepository memory;
    private final TransactionTemplate tx;
    private final ForgetRecoveryRepository recovery;
    private final ForgetLedger ledger;
    private final TaskEventHub events;
    private final ProfileDocuments documents;

    public MemoryForgetService(
            MemoryRepository memory,
            TransactionTemplate tx,
            ForgetRecoveryRepository recovery,
            ForgetLedger ledger,
            TaskEventHub events) {
        this(memory, tx, recovery, ledger, events, ProfileDocuments.disabled());
    }

    public MemoryForgetService(
            MemoryRepository memory,
            TransactionTemplate tx,
            ForgetRecoveryRepository recovery,
            ForgetLedger ledger,
            TaskEventHub events,
            ProfileDocuments documents) {
        this.events = Objects.requireNonNull(events);
        this.memory = Objects.requireNonNull(memory);
        this.tx = Objects.requireNonNull(tx);
        this.recovery = Objects.requireNonNull(recovery);
        this.ledger = Objects.requireNonNull(ledger);
        this.documents = Objects.requireNonNull(documents);
    }

    /** 执行遗忘。独立账本模式分为主库准备、账本追加、主库应用三阶段；任何未知结果都保留阻断屏障，并用同一事件重试。 */
    public long forget(Owner owner, long expectedEpoch, String key) {
        new IdempotencyKey(key);
        String hash = ForgetFact.hash(expectedEpoch);
        if (!ledger.enabled()) {
            return localForget(owner, expectedEpoch, key, hash);
        }
        requireNoTransaction();
        // 阶段一：先提交主库屏障，后续账本超时也不能继续读取或写入旧记忆。
        var prepared =
                tx.execute(
                        status -> {
                            var binding = recovery.lockBinding();
                            if (!binding.active()) {
                                throw new Problem(503, "forget_ledger_uninitialized");
                            }
                            memory.ensure(owner);
                            var privacy = memory.lock(owner);
                            var receipt = memory.operation(owner, key);
                            if (receipt != null) {
                                // 幂等重放也擦除可能从旧磁盘备份恢复的历史 epoch 文件。
                                long replay = Long.parseLong(receipt.replay(hash));
                                documents.eraseThrough(owner, expectedEpoch);
                                return new Prepared(null, replay);
                            }
                            var pending = recovery.pending(owner);
                            if (pending != null) {
                                if (!pending.key().equals(key)) {
                                    throw new Problem(409, "forget_pending");
                                }
                                if (!pending.requestHash().equals(hash)) {
                                    throw new Problem(409, "idempotency_conflict");
                                }
                                return new Prepared(pending, null);
                            }
                            privacy.requireEpoch(expectedEpoch);
                            var fact =
                                    new ForgetFact(
                                            UUID.randomUUID().toString(),
                                            binding.lineage(),
                                            owner,
                                            expectedEpoch,
                                            key,
                                            hash);
                            recovery.prepare(fact);
                            events.invalidateOwner(owner);
                            return new Prepared(fact, null);
                        });
        if (prepared.receipt() != null) {
            return prepared.receipt();
        }
        var fact = prepared.fact();
        try {
            // 阶段二：账本使用独立提交；超时只表示结果未知，不能解除主库屏障或更换事件编号。
            ledger.append(fact);
        } catch (RuntimeException failure) {
            throw new Problem(503, "forget_ledger_unavailable");
        }
        // 阶段三：确认账本绑定未改变，再按同一事实擦除主库并写入幂等回执。
        return tx.execute(
                status -> {
                    var binding = recovery.lockBinding();
                    if (!binding.active() || !binding.lineage().equals(fact.lineage())) {
                        throw new Problem(503, "forget_ledger_binding_changed");
                    }
                    var privacy = memory.lock(owner);
                    var receipt = memory.operation(owner, key);
                    if (receipt != null) {
                        long replay = Long.parseLong(receipt.replay(hash));
                        documents.eraseThrough(owner, fact.fromEpoch());
                        return replay;
                    }
                    if (!fact.equals(recovery.pending(owner))) {
                        throw new Problem(409, "forget_pending_changed");
                    }
                    privacy.requireEpoch(fact.fromEpoch());
                    // 文件先擦除，成功后才允许 SQL apply 删除持久 pending；失败继续保持屏障。
                    documents.eraseThrough(owner, fact.fromEpoch());
                    recovery.apply(fact);
                    return fact.toEpoch();
                });
    }

    /** 未启用外部账本时在主库原子擦除。已经绑定启用账本的主库不能降级到本地模式，以免遗漏独立遗忘事实。 */
    private long localForget(Owner owner, long expectedEpoch, String key, String hash) {
        return tx.execute(
                status -> {
                    if (recovery.lockBinding().active()) {
                        throw new Problem(503, "forget_ledger_required");
                    }
                    memory.ensure(owner);
                    var privacy = memory.lock(owner);
                    var receipt = memory.operation(owner, key);
                    if (receipt != null) {
                        long replay = Long.parseLong(receipt.replay(hash));
                        documents.eraseThrough(owner, expectedEpoch);
                        return replay;
                    }
                    privacy.requireEpoch(expectedEpoch);
                    // 文件系统不参加 SQL 回滚；先删后提交只会损失可用性，不能复活已遗忘资料。
                    documents.eraseThrough(owner, expectedEpoch);
                    memory.forget(owner);
                    events.invalidateOwner(owner);
                    memory.eraseProfile(owner);
                    memory.eraseSources(owner);
                    memory.eraseFacts(owner);
                    memory.eraseSnapshots(owner);
                    memory.eraseWorkContexts(owner);
                    memory.eraseIndex(owner);
                    memory.eraseJobs(owner);
                    memory.audit(owner, privacy.epoch() + 1, "forget", "all");
                    memory.recordOperation(owner, key, hash, Long.toString(privacy.epoch() + 1));
                    return privacy.epoch() + 1;
                });
    }

    /** 只启用画像时，同步 SQL 擦除结束后直接完成屏障，无需依赖证据 Worker。 */
    public void completeLocalPurge(Owner owner, long expectedEpoch) {
        tx.executeWithoutResult(
                status -> {
                    var privacy = memory.lock(owner);
                    // 旧幂等回执只能完成它绑定的 epoch，不能解除用户后来产生的遗忘屏障。
                    if (privacy == null || privacy.epoch() != expectedEpoch) {
                        return;
                    }
                    // 即使 epoch 相等，仍在等待独立账本的持久请求也必须继续阻断访问。
                    if (memory.purged(owner) == 1) {
                        memory.audit(owner, expectedEpoch, "purge_complete", "all");
                    }
                });
    }

    static void requireNoTransaction() {
        if (TransactionSynchronizationManager.isActualTransactionActive()) {
            throw new IllegalStateException("Forget orchestration requires no ambient transaction");
        }
    }

    private record Prepared(ForgetFact fact, Long receipt) {}
}
