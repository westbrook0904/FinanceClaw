package com.financeclaw.api;

import com.financeclaw.app.scheduling.TriggerService;
import com.financeclaw.domain.Task.Problem;
import com.financeclaw.domain.scheduling.TriggerFiring;
import com.financeclaw.domain.scheduling.TriggerRule;
import jakarta.servlet.http.HttpServletRequest;
import java.time.Instant;
import java.util.List;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/v1/schedules")
public final class ScheduleController {
    private final TriggerService schedules;

    public ScheduleController(TriggerService schedules) {
        this.schedules = schedules;
    }

    public record Update(Long expectedRevision, TriggerRule.Definition definition) {}

    @PutMapping("/{id}")
    public TriggerRule put(
            HttpServletRequest request, @PathVariable String id, @RequestBody Update body) {
        BearerIdentity.requirePermission(request, "schedules:manage");
        if (body == null || body.expectedRevision() == null || body.definition() == null)
            throw new Problem(422, "invalid_schedule");
        return schedules.put(
                TaskController.owner(request), id, body.expectedRevision(), body.definition());
    }

    @GetMapping
    public List<TriggerRule> list(
            HttpServletRequest request,
            @RequestParam(defaultValue = "") String after,
            @RequestParam(defaultValue = "50") int limit) {
        BearerIdentity.requirePermission(request, "schedules:manage");
        return schedules.list(TaskController.owner(request), after, limit);
    }

    @GetMapping("/{id}")
    public TriggerRule get(HttpServletRequest request, @PathVariable String id) {
        BearerIdentity.requirePermission(request, "schedules:manage");
        return schedules.get(TaskController.owner(request), id);
    }

    @GetMapping("/{id}/history")
    public List<TriggerFiring> history(
            HttpServletRequest request,
            @PathVariable String id,
            @RequestParam(defaultValue = "1970-01-01T00:00:00Z") Instant after,
            @RequestParam(name = "after_key", defaultValue = "") String afterKey,
            @RequestParam(defaultValue = "50") int limit) {
        BearerIdentity.requirePermission(request, "schedules:manage");
        return schedules.history(TaskController.owner(request), id, after, afterKey, limit);
    }

    @PostMapping("/{id}/events")
    public TriggerFiring fire(
            HttpServletRequest request,
            @PathVariable String id,
            @RequestHeader(value = "Idempotency-Key", required = false) String key,
            @RequestBody(required = false) com.fasterxml.jackson.databind.JsonNode body) {
        BearerIdentity.requirePermission(request, "schedules:manage");
        if (body != null && (!body.isObject() || !body.isEmpty()))
            throw new Problem(422, "invalid_body");
        return schedules.webhook(TaskController.owner(request), id, key);
    }
}
