package com.financeclaw.app.operations.port;

import com.financeclaw.domain.Operations.*;
import com.financeclaw.domain.Task.Owner;
import java.util.List;
import java.util.Map;

/** Owner-scoped operational projections, without SQL row types or tool/message payloads. */
public interface OperationsReadGateway {
  record Overview(
      Map<String, Object> tasks,
      Map<String, Object> calls,
      Map<String, Object> outbox,
      Map<String, Object> notifications) {}

  record DrainSnapshot(Admission admission, Map<String, Long> counts) {}

  Overview overview(Owner owner);

  /** Admission and every count must be observed in one database snapshot. */
  DrainSnapshot drain(Owner owner);

  List<TurnBacklog> turns(Owner owner, String after, int limit);

  List<NotificationBacklog> notifications(Owner owner, String after, int limit);

  List<BindingBacklog> bindings(Owner owner, String after, int limit);

  List<MemoryBacklog> memoryJobs(Owner owner, long after, int limit);
}
