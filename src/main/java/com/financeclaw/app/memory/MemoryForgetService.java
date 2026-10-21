package com.financeclaw.app.memory;

import com.financeclaw.app.memory.port.*;
import com.financeclaw.domain.IdempotencyKey;
import com.financeclaw.domain.Task.*;
import com.financeclaw.domain.memory.ForgetFact;
import java.util.UUID;
import org.springframework.transaction.support.TransactionSynchronizationManager;
import org.springframework.transaction.support.TransactionTemplate;

/** A durable local barrier bridges two independent Spring transactions without claiming XA. */
public final class MemoryForgetService {
  private final MemoryRepository memory;
  private final TransactionTemplate tx;
  private final ForgetRecoveryRepository recovery;
  private final ForgetLedger ledger;

  public MemoryForgetService(
      MemoryRepository memory,
      TransactionTemplate tx,
      ForgetRecoveryRepository recovery,
      ForgetLedger ledger) {
    this.memory = memory;
    this.tx = tx;
    this.recovery = recovery;
    this.ledger = ledger;
  }

  /** In-process legacy fixtures; production always supplies the durable binding repository. */
  public MemoryForgetService(MemoryRepository memory, TransactionTemplate tx) {
    this(memory, tx, null, null);
  }

  public long forget(Owner owner, long expectedEpoch, String key) {
    new IdempotencyKey(key);
    String hash = ForgetFact.hash(expectedEpoch);
    if (ledger == null || !ledger.enabled()) return legacy(owner, expectedEpoch, key, hash);
    requireNoTransaction();
    var prepared =
        tx.execute(
            status -> {
              var binding = recovery.lockBinding();
              if (!binding.active()) throw new Problem(503, "forget_ledger_uninitialized");
              memory.ensure(owner);
              var privacy = memory.lock(owner);
              var receipt = memory.operation(owner, key);
              if (receipt != null) return new Prepared(null, Long.parseLong(receipt.replay(hash)));
              var pending = recovery.pending(owner);
              if (pending != null) {
                if (!pending.key().equals(key)) throw new Problem(409, "forget_pending");
                if (!pending.requestHash().equals(hash))
                  throw new Problem(409, "idempotency_conflict");
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
              return new Prepared(fact, null);
            });
    if (prepared.receipt() != null) return prepared.receipt();
    var fact = prepared.fact();
    try {
      // The primary transaction is already committed. Unknown outcomes retain its barrier.
      ledger.append(fact);
    } catch (RuntimeException failure) {
      throw new Problem(503, "forget_ledger_unavailable");
    }
    return tx.execute(
        status -> {
          var binding = recovery.lockBinding();
          if (!binding.active() || !binding.lineage().equals(fact.lineage()))
            throw new Problem(503, "forget_ledger_binding_changed");
          var privacy = memory.lock(owner);
          var receipt = memory.operation(owner, key);
          if (receipt != null) return Long.parseLong(receipt.replay(hash));
          if (!fact.equals(recovery.pending(owner)))
            throw new Problem(409, "forget_pending_changed");
          privacy.requireEpoch(fact.fromEpoch());
          recovery.apply(fact);
          return fact.toEpoch();
        });
  }

  private long legacy(Owner owner, long expectedEpoch, String key, String hash) {
    return tx.execute(
        status -> {
          if (recovery != null && recovery.lockBinding().active())
            throw new Problem(503, "forget_ledger_required");
          memory.ensure(owner);
          var privacy = memory.lock(owner);
          var receipt = memory.operation(owner, key);
          if (receipt != null) return Long.parseLong(receipt.replay(hash));
          privacy.requireEpoch(expectedEpoch);
          memory.forget(owner);
          memory.eraseSources(owner);
          memory.eraseFacts(owner);
          memory.eraseRecalls(owner);
          memory.eraseIndex(owner);
          memory.eraseJobs(owner);
          memory.audit(owner, privacy.epoch() + 1, "forget", "all");
          memory.recordOperation(owner, key, hash, Long.toString(privacy.epoch() + 1));
          return privacy.epoch() + 1;
        });
  }

  static void requireNoTransaction() {
    if (TransactionSynchronizationManager.isActualTransactionActive())
      throw new IllegalStateException("Forget orchestration requires no ambient transaction");
  }

  private record Prepared(ForgetFact fact, Long receipt) {}
}
