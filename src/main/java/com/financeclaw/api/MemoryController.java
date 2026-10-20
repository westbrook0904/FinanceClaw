package com.financeclaw.api;

import com.financeclaw.app.memory.MemoryService;
import com.financeclaw.domain.Memory.*;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.validation.Valid;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotNull;
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

  public record Decision(@Min(1) long revision, @NotNull Boolean accept) {}

  public record Forget(@Min(1) long epoch) {}

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
      @Valid @RequestBody Decision d) {
    return Map.of(
        "status", service.decide(TaskController.owner(r), id, d.revision(), d.accept(), key));
  }

  @PostMapping("/forget")
  public Map<String, Long> forget(
      HttpServletRequest r,
      @RequestHeader(value = "Idempotency-Key", required = false) String key,
      @Valid @RequestBody Forget f) {
    return Map.of("epoch", service.forget(TaskController.owner(r), f.epoch(), key));
  }

  @PostMapping("/rebuild")
  public Map<String, Integer> rebuild(
      HttpServletRequest r,
      @RequestHeader(value = "Idempotency-Key", required = false) String key) {
    return Map.of("queued", service.rebuild(TaskController.owner(r), key));
  }
}
