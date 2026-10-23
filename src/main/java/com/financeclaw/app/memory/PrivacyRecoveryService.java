package com.financeclaw.app.memory;

import com.financeclaw.app.memory.port.*;
import com.financeclaw.domain.ExecutionScope;
import com.financeclaw.domain.Memory.Privacy;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.memory.ForgetFact;
import java.util.*;
import org.springframework.transaction.support.TransactionTemplate;

/** 离线隐私恢复入口：应用恢复的备份必须先与独立遗忘账本逐项核对；重放、残留检查及最终快照复核完成前保持写入和发送隔离。 */
public final class PrivacyRecoveryService {
    private final MemoryRepository memory;
    private final ForgetRecoveryRepository recovery;
    private final ForgetLedger ledger;
    private final TransactionTemplate tx;

    public PrivacyRecoveryService(
            MemoryRepository memory,
            ForgetRecoveryRepository recovery,
            ForgetLedger ledger,
            TransactionTemplate tx) {
        this.memory = memory;
        this.recovery = recovery;
        this.ledger = ledger;
        this.tx = tx;
    }

    /** 在所有写入方停止的前提下初始化独立账本；必须先检查本地主库无残留冲突，再绑定账本并激活。 */
    public Report bootstrap() {
        requireLedger();
        return tx.execute(
                status -> {
                    var binding = recovery.lockBinding();
                    if (!recovery.pending().isEmpty() || recovery.unsafeRows() != 0) {
                        throw new IllegalStateException("privacy_bootstrap_unsafe");
                    }
                    var history = recovery.history();
                    var epochs = epochs(binding.lineage(), history);
                    validateOwners(recovery.owners(), epochs, true);
                    // 仅允许离线执行：持有账本绑定锁，防止读取历史到激活之间插入新的遗忘事实。
                    ledger.initialize(binding.lineage(), history);
                    if (!new HashSet<>(ledger.snapshot(binding.lineage()))
                            .equals(new HashSet<>(history))) {
                        throw new IllegalStateException("privacy_ledger_ahead_replay_required");
                    }
                    recovery.activate();
                    return report("privacy_ledger_initialized", history);
                });
    }

    /** 对照独立账本重放遗忘并完成残留验证。追加中的不确定事实复用原事件身份，最后再读取账本排除恢复期间的漂移。 */
    public Report replay() {
        requireLedger();
        var binding = tx.execute(status -> recovery.binding());
        if (!binding.active()) {
            throw new IllegalStateException("forget_ledger_uninitialized");
        }
        var before = ledger.snapshot(binding.lineage());
        var beforeEpochs = epochs(binding.lineage(), before);
        var prepared =
                tx.execute(
                        status -> {
                            if (!binding.equals(recovery.lockBinding())) {
                                throw new IllegalStateException("privacy_binding_changed");
                            }
                            var owners = recovery.owners();
                            validateOwners(owners, beforeEpochs, false);
                            if (!new HashSet<>(before).containsAll(recovery.history())) {
                                throw new IllegalStateException("privacy_local_history_conflict");
                            }
                            var pending = recovery.pending();
                            for (var fact : pending) {
                                var owner =
                                        owners.stream()
                                                .filter(
                                                        p ->
                                                                p.tenant()
                                                                                .equals(
                                                                                        fact.owner()
                                                                                                .tenant())
                                                                        && p.subject()
                                                                                .equals(
                                                                                        fact.owner()
                                                                                                .subject()))
                                                .findFirst()
                                                .orElseThrow();
                                if (!fact.lineage().equals(binding.lineage())
                                        || !owner.purgePending()
                                        || fact.fromEpoch() != owner.epoch()
                                        || memory.operation(fact.owner(), fact.key()) != null) {
                                    throw new IllegalStateException("privacy_pending_conflict");
                                }
                            }
                            return pending;
                        });
        // 准备中的请求已经获得授权；补交结果未知的追加操作时必须复用原事件身份。
        for (var fact : prepared) {
            ledger.append(fact);
        }
        var facts = ledger.snapshot(binding.lineage());
        var epochs = epochs(binding.lineage(), facts);
        tx.executeWithoutResult(
                status -> {
                    if (!binding.equals(recovery.lockBinding())) {
                        throw new IllegalStateException("privacy_binding_changed");
                    }
                    validateOwners(recovery.owners(), epochs, false);
                    var authority = new HashSet<>(facts);
                    if (!authority.containsAll(recovery.history())
                            || !authority.containsAll(recovery.pending())) {
                        throw new IllegalStateException("privacy_local_history_conflict");
                    }
                    for (var fact : facts) {
                        memory.ensure(fact.owner());
                        memory.lock(fact.owner());
                        recovery.apply(fact);
                    }
                    for (var owner : epochs.keySet()) {
                        recovery.complete(owner);
                    }
                    if (recovery.unsafeRows() != 0) {
                        throw new IllegalStateException("privacy_replay_unsafe");
                    }
                    validateOwners(recovery.owners(), epochs, true);
                });
        if (!facts.equals(ledger.snapshot(binding.lineage()))) {
            throw new IllegalStateException("privacy_ledger_changed_keep_isolated");
        }
        return report("privacy_replayed", facts);
    }

    private void requireLedger() {
        MemoryForgetService.requireNoTransaction();
        if (!ledger.enabled()) {
            throw new IllegalStateException("forget_ledger_required");
        }
    }

    private static Map<Owner, Long> epochs(String lineage, List<ForgetFact> facts) {
        var epochs = new HashMap<Owner, Long>();
        var ids = new HashSet<String>();
        var keys = new HashSet<List<String>>();
        for (var fact : facts) {
            if (!fact.lineage().equals(lineage)
                    || !ids.add(fact.eventId())
                    || !keys.add(List.of(fact.owner().tenant(), fact.owner().subject(), fact.key()))
                    || fact.fromEpoch() != epochs.getOrDefault(fact.owner(), 1L)) {
                throw new IllegalStateException("privacy_ledger_invalid_history");
            }
            epochs.put(fact.owner(), fact.toEpoch());
        }
        return epochs;
    }

    private static void validateOwners(
            List<Privacy> owners, Map<Owner, Long> epochs, boolean exact) {
        for (var owner : owners) {
            long latest = epochs.getOrDefault(new Owner(owner.tenant(), owner.subject()), 1L);
            if (owner.epoch() < 1
                    || owner.epoch() > latest
                    || (exact && (owner.epoch() != latest || owner.purgePending()))) {
                throw new IllegalStateException("privacy_owner_epoch_conflict");
            }
        }
    }

    private static Report report(String result, List<ForgetFact> facts) {
        return new Report(
                result,
                facts.size(),
                facts.stream().map(ForgetFact::owner).distinct().count(),
                ExecutionScope.hash(facts.stream().map(ForgetFact::toString).toList()));
    }

    /** 运维报告只含计数与摘要，不泄露用户身份、原始请求键、正文、SQL 或凭据。 */
    public record Report(String result, int facts, long owners, String fingerprint) {}
}
