package com.financeclaw.app.operations.port;

import com.financeclaw.domain.Operations.*;
import com.financeclaw.domain.Task.Owner;

/**
 * Admission facts. Lock, compare-and-set and receipt must share the caller's Spring transaction.
 */
public interface AdmissionGateway {
  Admission current(Owner owner);

  /** Create the default row if absent, then hold its exclusive lock until the transaction ends. */
  Admission lock(Owner owner);

  Change findChange(Owner owner, String key);

  boolean update(Owner owner, Admission next, long expectedRevision);

  void record(Owner owner, AdmissionChange request, Admission next);
}
