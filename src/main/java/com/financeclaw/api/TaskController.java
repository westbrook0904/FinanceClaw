package com.financeclaw.api;

import com.fasterxml.jackson.databind.JsonNode;
import com.financeclaw.api.request.SubmitTurnRequest;
import com.financeclaw.app.task.CancelTaskService;
import com.financeclaw.app.task.SubmitTaskService;
import com.financeclaw.app.task.TaskQueryService;
import com.financeclaw.domain.Task.*;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.validation.Valid;
import java.util.List;
import java.util.Map;
import org.springframework.http.*;
import org.springframework.http.codec.ServerSentEvent;
import org.springframework.web.bind.annotation.*;
import reactor.core.publisher.Flux;

@RestController
@RequestMapping("/v1/conversations")
public class TaskController {
  private final SubmitTaskService submission;
  private final TaskQueryService queries;
  private final CancelTaskService cancellation;
  private final TaskEvents streams;

  public TaskController(
      SubmitTaskService submission,
      TaskQueryService queries,
      CancelTaskService cancellation,
      TaskEvents streams) {
    this.submission = submission;
    this.queries = queries;
    this.cancellation = cancellation;
    this.streams = streams;
  }

  static Owner owner(HttpServletRequest request) {
    return (Owner) request.getAttribute(BearerIdentity.ATTRIBUTE);
  }

  @PostMapping
  public ResponseEntity<?> create(HttpServletRequest request, @RequestBody JsonNode body) {
    if (!body.isObject() || !body.isEmpty()) throw new Problem(422, "invalid_body");
    return ResponseEntity.status(201)
        .body(Map.of("conversation_id", submission.createConversation(owner(request))));
  }

  @PostMapping("/{conversation}/turns")
  public ResponseEntity<Accepted> submit(
      HttpServletRequest request,
      @PathVariable String conversation,
      @RequestHeader(value = "Idempotency-Key", required = false) String key,
      @Valid @RequestBody SubmitTurnRequest body) {
    return ResponseEntity.accepted()
        .body(submission.submit(owner(request), conversation, key, body.message()));
  }

  public record Snapshot(
      String conversationId,
      String turnId,
      String status,
      long revision,
      String result,
      String reason) {}

  @GetMapping("/{conversation}/turns/{turn}")
  public Snapshot get(
      HttpServletRequest request, @PathVariable String conversation, @PathVariable String turn) {
    Turn t = queries.get(owner(request), conversation, turn);
    return new Snapshot(
        t.conversationId(), t.id(), t.status(), t.revision(), t.result(), t.reason());
  }

  @PostMapping("/{conversation}/turns/{turn}/cancel")
  public ResponseEntity<Accepted> cancel(
      HttpServletRequest request,
      @PathVariable String conversation,
      @PathVariable String turn,
      @RequestHeader(value = "Idempotency-Key", required = false) String key,
      @RequestBody(required = false) JsonNode body) {
    if (body != null && (!body.isObject() || !body.isEmpty()))
      throw new Problem(422, "invalid_body");
    return ResponseEntity.accepted()
        .body(cancellation.cancel(owner(request), conversation, turn, key));
  }

  @GetMapping("/{conversation}/messages")
  public List<Journal> messages(
      HttpServletRequest request,
      @PathVariable String conversation,
      @RequestParam(defaultValue = "0") long after,
      @RequestParam(defaultValue = "100") int limit) {
    return queries.messages(owner(request), conversation, after, limit);
  }

  @GetMapping(
      value = "/{conversation}/turns/{turn}/events",
      produces = MediaType.TEXT_EVENT_STREAM_VALUE)
  public ResponseEntity<Flux<ServerSentEvent<Event>>> events(
      HttpServletRequest request,
      @PathVariable String conversation,
      @PathVariable String turn,
      @RequestHeader(value = "Last-Event-ID", defaultValue = "0") long after) {
    return ResponseEntity.ok()
        .header("Cache-Control", "no-cache")
        .header("X-Accel-Buffering", "no")
        .body(streams.open(owner(request), conversation, turn, after));
  }
}
