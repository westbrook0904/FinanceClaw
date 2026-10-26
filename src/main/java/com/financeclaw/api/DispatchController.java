package com.financeclaw.api;

import com.financeclaw.app.scheduling.DispatchPolicyService;
import com.financeclaw.domain.scheduling.ConcurrencyLimit;
import com.financeclaw.domain.scheduling.DispatchPolicy;
import jakarta.servlet.http.HttpServletRequest;
import java.util.Map;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/v1/dispatch")
public final class DispatchController {
    private final DispatchPolicyService policies;

    public DispatchController(DispatchPolicyService policies) {
        this.policies = policies;
    }

    @GetMapping("/policy")
    public DispatchPolicy get(HttpServletRequest r) {
        BearerIdentity.requirePermission(r, "schedules:manage");
        return policies.get(TaskController.owner(r));
    }

    public record PolicyUpdate(Long revision, Integer maximumRunning, Integer maximumPerAgent) {}

    public record LimitUpdate(Long revision, Integer maximumRunning) {}

    @PutMapping("/policy")
    public DispatchPolicy update(HttpServletRequest r, @RequestBody PolicyUpdate p) {
        BearerIdentity.requirePermission(r, "schedules:manage");
        if (p == null
                || p.revision() == null
                || p.maximumRunning() == null
                || p.maximumPerAgent() == null)
            throw new com.financeclaw.domain.Task.Problem(422, "invalid_dispatch_policy");
        return policies.update(
                TaskController.owner(r),
                new DispatchPolicy(p.revision(), p.maximumRunning(), p.maximumPerAgent()));
    }

    public record Priority(Integer priority) {}

    @GetMapping("/tenant-policy")
    public ConcurrencyLimit tenant(HttpServletRequest r) {
        BearerIdentity.requirePermission(r, "runtime:manage");
        return policies.tenant(TaskController.owner(r));
    }

    @PutMapping("/tenant-policy")
    public ConcurrencyLimit tenant(HttpServletRequest r, @RequestBody LimitUpdate limit) {
        BearerIdentity.requirePermission(r, "runtime:manage");
        return policies.updateTenant(TaskController.owner(r), limit(limit));
    }

    @GetMapping("/agents/{agent}/policy")
    public ConcurrencyLimit agent(HttpServletRequest r, @PathVariable String agent) {
        BearerIdentity.requirePermission(r, "runtime:manage");
        return policies.agent(TaskController.owner(r), agent);
    }

    @PutMapping("/agents/{agent}/policy")
    public ConcurrencyLimit agent(
            HttpServletRequest r, @PathVariable String agent, @RequestBody LimitUpdate limit) {
        BearerIdentity.requirePermission(r, "runtime:manage");
        return policies.updateAgent(TaskController.owner(r), agent, limit(limit));
    }

    @PutMapping("/turns/{turn}/priority")
    public Map<String, Boolean> priority(
            HttpServletRequest r, @PathVariable String turn, @RequestBody Priority p) {
        BearerIdentity.requirePermission(r, "schedules:manage");
        if (p == null || p.priority() == null)
            throw new com.financeclaw.domain.Task.Problem(422, "invalid_priority");
        policies.prioritize(TaskController.owner(r), turn, p.priority());
        return Map.of("updated", true);
    }

    private static ConcurrencyLimit limit(LimitUpdate value) {
        if (value == null || value.revision() == null || value.maximumRunning() == null)
            throw new com.financeclaw.domain.Task.Problem(422, "invalid_concurrency_limit");
        return new ConcurrencyLimit(value.revision(), value.maximumRunning());
    }
}
