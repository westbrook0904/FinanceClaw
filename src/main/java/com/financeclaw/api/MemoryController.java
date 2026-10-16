package com.financeclaw.api;

import com.financeclaw.app.memory.MemoryService;
import com.financeclaw.domain.Memory.*;
import com.financeclaw.domain.Task.Problem;
import jakarta.servlet.http.HttpServletRequest;
import java.util.*;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/v1/memory")
public class MemoryController {
  private final MemoryService service;
  private final com.financeclaw.app.memory.MemorySearchService search;

  public MemoryController(
      MemoryService service, com.financeclaw.app.memory.MemorySearchService search) {
    this.service = service;
    this.search = search;
  }

  @GetMapping("/search")
  public List<Fact> search(HttpServletRequest r, @RequestParam(required = false) String query) {
    return search.search(TaskController.owner(r), query);
  }

  public record Decision(long revision, Boolean accept) {}

  public record Forget(long epoch) {}

  @GetMapping
  public List<Fact> list(HttpServletRequest r) {
    return service.list(TaskController.owner(r));
  }

  @GetMapping("/privacy")
  public Privacy privacy(HttpServletRequest r) {
    return service.privacy(TaskController.owner(r));
  }

  @PostMapping("/{id}/decision")
  public Map<String, String> decide(
      HttpServletRequest r,
      @PathVariable String id,
      @RequestHeader(value = "Idempotency-Key", required = false) String key,
      @RequestBody Decision d) {
    if (d == null || d.accept() == null || d.revision() < 1)
      throw new Problem(422, "invalid_memory_decision");
    return Map.of(
        "status", service.decide(TaskController.owner(r), id, d.revision(), d.accept(), key));
  }

  @PostMapping("/forget")
  public Map<String, Long> forget(
      HttpServletRequest r,
      @RequestHeader(value = "Idempotency-Key", required = false) String key,
      @RequestBody Forget f) {
    if (f == null || f.epoch() < 1) throw new Problem(422, "invalid_memory_epoch");
    return Map.of("epoch", service.forget(TaskController.owner(r), f.epoch(), key));
  }

  @PostMapping("/rebuild")
  public Map<String, Integer> rebuild(
      HttpServletRequest r,
      @RequestHeader(value = "Idempotency-Key", required = false) String key) {
    return Map.of("queued", service.rebuild(TaskController.owner(r), key));
  }
}
