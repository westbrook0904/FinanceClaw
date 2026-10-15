package com.financeclaw.api;

import com.financeclaw.infrastructure.task.OperationsMapper;
import jakarta.servlet.http.HttpServletRequest;
import java.util.Map;
import org.springframework.web.bind.annotation.*;

@RestController
public class OperationsController {
  private final OperationsMapper db;

  public OperationsController(OperationsMapper db) {
    this.db = db;
  }

  @GetMapping("/v1/operations")
  public Map<String, Object> view(HttpServletRequest request) {
    var owner = TaskController.owner(request);
    return Map.of(
        "tasks",
        db.tasks(owner),
        "calls",
        db.calls(owner),
        "outbox",
        db.outbox(owner),
        "notifications",
        db.notifications(owner),
        "provider_usage",
        "unavailable",
        "provider_cost",
        "unavailable");
  }
}
