package com.financeclaw.infrastructure.operations;

import com.financeclaw.app.operations.port.OperationsReadGateway;
import com.financeclaw.domain.Operations.*;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.infrastructure.task.OperationsMapper;
import java.util.Collections;
import java.util.List;
import java.util.TreeMap;
import org.springframework.stereotype.Repository;

@Repository
public class MyBatisOperationsReadGateway implements OperationsReadGateway {
  private final OperationsMapper operations;

  public MyBatisOperationsReadGateway(OperationsMapper operations) {
    this.operations = operations;
  }

  @Override
  public Overview overview(Owner owner) {
    return new Overview(
        operations.tasks(owner),
        operations.calls(owner),
        operations.outbox(owner),
        operations.notifications(owner));
  }

  @Override
  public DrainSnapshot drain(Owner owner) {
    // Keep this as one SQL statement so admission and all counts share one PostgreSQL MVCC
    // snapshot.
    var row = operations.drain(owner);
    var state =
        new Admission((Boolean) row.get("paused"), ((Number) row.get("revision")).longValue());
    var counts = new TreeMap<String, Long>();
    row.forEach(
        (key, value) -> {
          if (!key.equals("paused") && !key.equals("revision"))
            counts.put(key, ((Number) value).longValue());
        });
    return new DrainSnapshot(state, Collections.unmodifiableMap(counts));
  }

  @Override
  public List<TurnBacklog> turns(Owner owner, String after, int limit) {
    return operations.turnBacklog(owner, after, limit);
  }

  @Override
  public List<NotificationBacklog> notifications(Owner owner, String after, int limit) {
    return operations.notificationBacklog(owner, after, limit);
  }

  @Override
  public List<BindingBacklog> bindings(Owner owner, String after, int limit) {
    return operations.bindingBacklog(owner, after, limit);
  }

  @Override
  public List<MemoryBacklog> memoryJobs(Owner owner, long after, int limit) {
    return operations.memoryBacklog(owner, after, limit);
  }
}
