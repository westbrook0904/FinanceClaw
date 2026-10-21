package com.financeclaw.app.memory;

import com.financeclaw.app.memory.port.*;
import com.financeclaw.domain.ExecutionScope;
import com.financeclaw.domain.Memory.Privacy;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.memory.ForgetFact;
import java.util.*;
import org.springframework.transaction.support.TransactionTemplate;

/** Offline use cases. Operators keep every writer and outbound role stopped throughout. */
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

  public Report bootstrap() {
    requireLedger();
    return tx.execute(
        status -> {
          var binding = recovery.lockBinding();
          if (!recovery.pending().isEmpty() || recovery.unsafeRows() != 0)
            throw new IllegalStateException("privacy_bootstrap_unsafe");
          var history = recovery.history();
          var epochs = epochs(binding.lineage(), history);
          validateOwners(recovery.owners(), epochs, true);
          // Offline only: the binding lock prevents a new forget between snapshot and activation.
          ledger.initialize(binding.lineage(), history);
          if (!new HashSet<>(ledger.snapshot(binding.lineage())).equals(new HashSet<>(history)))
            throw new IllegalStateException("privacy_ledger_ahead_replay_required");
          recovery.activate();
          return report("privacy_ledger_initialized", history);
        });
  }

  public Report replay() {
    requireLedger();
    var binding = tx.execute(status -> recovery.binding());
    if (!binding.active()) throw new IllegalStateException("forget_ledger_uninitialized");
    var before = ledger.snapshot(binding.lineage());
    var beforeEpochs = epochs(binding.lineage(), before);
    var prepared =
        tx.execute(
            status -> {
              if (!binding.equals(recovery.lockBinding()))
                throw new IllegalStateException("privacy_binding_changed");
              var owners = recovery.owners();
              validateOwners(owners, beforeEpochs, false);
              if (!new HashSet<>(before).containsAll(recovery.history()))
                throw new IllegalStateException("privacy_local_history_conflict");
              var pending = recovery.pending();
              for (var fact : pending) {
                var owner =
                    owners.stream()
                        .filter(
                            p ->
                                p.tenant().equals(fact.owner().tenant())
                                    && p.subject().equals(fact.owner().subject()))
                        .findFirst()
                        .orElseThrow();
                if (!fact.lineage().equals(binding.lineage())
                    || !owner.purgePending()
                    || fact.fromEpoch() != owner.epoch()
                    || memory.operation(fact.owner(), fact.key()) != null)
                  throw new IllegalStateException("privacy_pending_conflict");
              }
              return pending;
            });
    // Prepared requests are already authorized; finish uncertain commits with the same identity.
    for (var fact : prepared) ledger.append(fact);
    var facts = ledger.snapshot(binding.lineage());
    var epochs = epochs(binding.lineage(), facts);
    tx.executeWithoutResult(
        status -> {
          if (!binding.equals(recovery.lockBinding()))
            throw new IllegalStateException("privacy_binding_changed");
          validateOwners(recovery.owners(), epochs, false);
          var authority = new HashSet<>(facts);
          if (!authority.containsAll(recovery.history())
              || !authority.containsAll(recovery.pending()))
            throw new IllegalStateException("privacy_local_history_conflict");
          for (var fact : facts) {
            memory.ensure(fact.owner());
            memory.lock(fact.owner());
            recovery.apply(fact);
          }
          for (var owner : epochs.keySet()) recovery.complete(owner);
          if (recovery.unsafeRows() != 0) throw new IllegalStateException("privacy_replay_unsafe");
          validateOwners(recovery.owners(), epochs, true);
        });
    if (!facts.equals(ledger.snapshot(binding.lineage())))
      throw new IllegalStateException("privacy_ledger_changed_keep_isolated");
    return report("privacy_replayed", facts);
  }

  private void requireLedger() {
    MemoryForgetService.requireNoTransaction();
    if (!ledger.enabled()) throw new IllegalStateException("forget_ledger_required");
  }

  private static Map<Owner, Long> epochs(String lineage, List<ForgetFact> facts) {
    var epochs = new HashMap<Owner, Long>();
    var ids = new HashSet<String>();
    var keys = new HashSet<List<String>>();
    for (var fact : facts) {
      if (!fact.lineage().equals(lineage)
          || !ids.add(fact.eventId())
          || !keys.add(List.of(fact.owner().tenant(), fact.owner().subject(), fact.key()))
          || fact.fromEpoch() != epochs.getOrDefault(fact.owner(), 1L))
        throw new IllegalStateException("privacy_ledger_invalid_history");
      epochs.put(fact.owner(), fact.toEpoch());
    }
    return epochs;
  }

  private static void validateOwners(List<Privacy> owners, Map<Owner, Long> epochs, boolean exact) {
    for (var owner : owners) {
      long latest = epochs.getOrDefault(new Owner(owner.tenant(), owner.subject()), 1L);
      if (owner.epoch() < 1
          || owner.epoch() > latest
          || (exact && (owner.epoch() != latest || owner.purgePending())))
        throw new IllegalStateException("privacy_owner_epoch_conflict");
    }
  }

  private static Report report(String result, List<ForgetFact> facts) {
    return new Report(
        result,
        facts.size(),
        facts.stream().map(ForgetFact::owner).distinct().count(),
        ExecutionScope.hash(facts.stream().map(ForgetFact::toString).toList()));
  }

  /** No identities, request keys, content, SQL or credentials in an operator report. */
  public record Report(String result, int facts, long owners, String fingerprint) {}
}
