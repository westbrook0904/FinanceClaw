package com.financeclaw.app.operations;

import com.financeclaw.app.operations.port.OperationsReadGateway;
import com.financeclaw.domain.Operations.*;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.Task.Problem;
import java.util.List;
import java.util.Map;
import org.springframework.stereotype.Service;

/** Query and pagination policy; database-specific projection mapping belongs to the gateway. */
@Service
public class OperationsQueryService {
  private final OperationsReadGateway operations;

  public OperationsQueryService(OperationsReadGateway operations) {
    this.operations = operations;
  }

  public Map<String, Object> view(Owner owner) {
    var facts = operations.overview(owner);
    return Map.of(
        "tasks",
        facts.tasks(),
        "calls",
        facts.calls(),
        "outbox",
        facts.outbox(),
        "notifications",
        facts.notifications(),
        "provider_usage",
        "unavailable",
        "provider_cost",
        "unavailable");
  }

  public Drain drain(Owner owner) {
    var snapshot = operations.drain(owner);
    return Drain.observed(snapshot.admission(), snapshot.counts());
  }

  public BacklogPage<?> backlog(Owner owner, String kind, String after, int limit) {
    if (limit < 1 || limit > 100 || after == null || !after.matches("[A-Za-z0-9_-]{0,128}"))
      throw new Problem(422, "invalid_pagination");
    return switch (kind) {
      case "turns" -> page(operations.turns(owner, after, limit + 1), limit);
      case "notifications" -> page(operations.notifications(owner, after, limit + 1), limit);
      case "notification_bindings" -> page(operations.bindings(owner, after, limit + 1), limit);
      case "memory_jobs" -> {
        long cursor;
        try {
          if (!after.isEmpty() && !after.matches("0|[1-9][0-9]*"))
            throw new NumberFormatException();
          cursor = after.isEmpty() ? 0 : Long.parseLong(after);
        } catch (NumberFormatException e) {
          throw new Problem(422, "invalid_pagination");
        }
        yield page(operations.memoryJobs(owner, cursor, limit + 1), limit);
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
