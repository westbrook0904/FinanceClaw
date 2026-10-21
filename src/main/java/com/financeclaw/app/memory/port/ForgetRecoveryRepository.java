package com.financeclaw.app.memory.port;

import com.financeclaw.domain.Memory.Privacy;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.memory.ForgetFact;
import java.util.List;

/** Local recovery state. Every operation joins the caller's Spring transaction. */
public interface ForgetRecoveryRepository {
  record Binding(String lineage, boolean active) {}

  Binding binding();

  Binding lockBinding();

  void activate();

  ForgetFact pending(Owner owner);

  List<ForgetFact> pending();

  void prepare(ForgetFact fact);

  /** Epoch-scoped, idempotent erasure and receipt; caller holds the owner lock. */
  void apply(ForgetFact fact);

  /** Existing authoritative forget audit plus operation receipts, for offline bootstrap. */
  List<ForgetFact> history();

  List<Privacy> owners();

  /** Complete purge after replay only; must refuse owners with pending intents. */
  void complete(Owner owner);

  /** Invalid epochs, orphan owners, old data/index/recall/jobs and pending intents. */
  long unsafeRows();
}
