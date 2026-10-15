package com.financeclaw.application;

import com.financeclaw.domain.ExecutionScope;
import com.financeclaw.domain.Operations.*;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.Task.Problem;
import com.financeclaw.infrastructure.task.*;
import java.util.*;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionTemplate;

/** Admission controls new Turns only; existing AgentScope work keeps its normal recovery path. */
@Service
public class OperationsService {
  private final TaskMapper tasks;
  private final OperationsMapper operations;
  private final TransactionTemplate tx;

  public OperationsService(TaskMapper tasks, OperationsMapper operations, TransactionTemplate tx) {
    this.tasks = tasks;
    this.operations = operations;
    this.tx = tx;
  }

  public Admission admission(Owner owner) {
    var value = tasks.admission(owner);
    return value == null ? new Admission(false, 0) : value;
  }

  public Receipt change(Owner owner, String key, boolean paused, long revision) {
    TaskService.validateKey(key);
    if (revision < 0 || revision == Long.MAX_VALUE) throw new Problem(422, "invalid_revision");
    String hash = ExecutionScope.hash(List.of(Boolean.toString(paused), Long.toString(revision)));
    return tx.execute(
        s -> {
          tasks.ensureAdmission(owner);
          var current = tasks.lockAdmission(owner);
          var old = tasks.admissionReplay(owner, key);
          if (old != null) {
            if (!old.requestHash().equals(hash)) throw new Problem(409, "idempotency_conflict");
            return new Receipt(old.paused(), old.revision(), true);
          }
          if (current.revision() != revision) throw new Problem(409, "admission_revision_changed");
          if (tasks.changeAdmission(owner, paused, revision) != 1)
            throw new Problem(409, "admission_revision_changed");
          tasks.recordAdmission(owner, key, hash, paused, revision + 1);
          return new Receipt(paused, revision + 1, false);
        });
  }

  public Drain drain(Owner owner) {
    // A single PostgreSQL statement gives admission and all counts the same MVCC snapshot.
    var row = operations.drain(owner);
    var state =
        new Admission(
            (Boolean) row.remove("paused"), ((Number) row.remove("revision")).longValue());
    var counts = new TreeMap<String, Long>();
    row.forEach((key, value) -> counts.put(key, ((Number) value).longValue()));
    return new Drain(
        "current_owner",
        state,
        Collections.unmodifiableMap(counts),
        state.paused() && counts.values().stream().allMatch(n -> n == 0));
  }

  public BacklogPage<?> backlog(Owner owner, String kind, String after, int limit) {
    if (limit < 1 || limit > 100 || after == null || !after.matches("[A-Za-z0-9_-]{0,128}"))
      throw new Problem(422, "invalid_pagination");
    return switch (kind) {
      case "turns" -> page(operations.turnBacklog(owner, after, limit + 1), limit);
      case "notifications" -> page(operations.notificationBacklog(owner, after, limit + 1), limit);
      case "notification_bindings" ->
          page(operations.bindingBacklog(owner, after, limit + 1), limit);
      case "memory_jobs" -> {
        long cursor;
        try {
          if (!after.isEmpty() && !after.matches("0|[1-9][0-9]*"))
            throw new NumberFormatException();
          cursor = after.isEmpty() ? 0 : Long.parseLong(after);
        } catch (NumberFormatException e) {
          throw new Problem(422, "invalid_pagination");
        }
        yield page(operations.memoryBacklog(owner, cursor, limit + 1), limit);
      }
      default -> throw new Problem(422, "invalid_backlog_kind");
    };
  }

  private <T extends BacklogItem> BacklogPage<T> page(List<T> rows, int limit) {
    boolean more = rows.size() > limit;
    var items = List.copyOf(rows.subList(0, Math.min(rows.size(), limit)));
    return new BacklogPage<>("current_owner", items, more ? items.getLast().id() : null);
  }
}
