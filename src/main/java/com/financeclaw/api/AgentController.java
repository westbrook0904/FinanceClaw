package com.financeclaw.api;

import com.financeclaw.app.agent.AgentService;
import com.financeclaw.app.agent.port.ResourceAdministration;
import com.financeclaw.domain.Task.Problem;
import com.financeclaw.domain.agent.AgentCapabilityCatalog;
import com.financeclaw.domain.agent.AgentComposition;
import com.financeclaw.domain.agent.AgentDraft;
import com.financeclaw.domain.release.Deployment;
import com.financeclaw.domain.release.DeploymentChange;
import jakarta.servlet.http.HttpServletRequest;
import java.util.List;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.*;

/** 普通调用者读目标目录；管理操作另外核验显式角色与资源 owner。 */
@RestController
@RequestMapping("/v1/agents")
public class AgentController {
    private final AgentService agents;
    private final ResourceAdministration access;

    public AgentController(AgentService agents) {
        this(agents, null);
    }

    @Autowired
    public AgentController(AgentService agents, ResourceAdministration access) {
        this.agents = agents;
        this.access = access;
    }

    public record Entry(String agentId, long draftRevision, boolean builtin) {}

    public record Create(
            String agentId,
            String displayName,
            String systemPrompt,
            AgentComposition composition) {}

    public record Update(
            Long expectedDraftRevision,
            String displayName,
            String systemPrompt,
            AgentComposition composition) {}

    public record Publish(Long expectedDraftRevision, Long expectedDeploymentRevision) {}

    @GetMapping
    public List<Entry> list(HttpServletRequest request) {
        return agents.list(TaskController.owner(request)).stream()
                .map(
                        agent ->
                                new Entry(
                                        agent.id(),
                                        agent.draftRevision(),
                                        "finance_agent".equals(agent.id())))
                .toList();
    }

    @PostMapping
    @ResponseStatus(HttpStatus.CREATED)
    public AgentDraft create(HttpServletRequest request, @RequestBody Create body) {
        var owner = BearerIdentity.requirePermission(request, "agent:manage");
        if (body == null) throw invalid();
        return agents.create(
                owner, body.agentId(), body.displayName(), body.systemPrompt(), body.composition());
    }

    @GetMapping("/{id}")
    public AgentDraft get(HttpServletRequest request, @PathVariable String id) {
        return agents.get(BearerIdentity.requirePermission(request, "agent:manage"), id);
    }

    /** PUT 是完整草稿替换；composition=null 明确恢复继承基础发布语义。 */
    @PutMapping("/{id}")
    public AgentDraft update(
            HttpServletRequest request, @PathVariable String id, @RequestBody Update body) {
        var owner = BearerIdentity.requirePermission(request, "agent:manage");
        if (body == null || body.expectedDraftRevision() == null) throw invalid();
        return agents.update(
                owner,
                id,
                body.expectedDraftRevision(),
                body.displayName(),
                body.systemPrompt(),
                body.composition());
    }

    @DeleteMapping("/{id}")
    @ResponseStatus(HttpStatus.NO_CONTENT)
    public void archive(
            HttpServletRequest request,
            @PathVariable String id,
            @RequestParam(required = false) Long expectedDraftRevision) {
        var owner = BearerIdentity.requirePermission(request, "agent:manage");
        if (expectedDraftRevision == null) throw invalid();
        if (access == null) throw new Problem(503, "service_unavailable");
        access.archive(owner, id, expectedDraftRevision);
    }

    @PostMapping("/{id}/publish")
    public Deployment publish(
            HttpServletRequest request, @PathVariable String id, @RequestBody Publish body) {
        var owner = BearerIdentity.requirePermission(request, "agent:manage");
        if (body == null
                || body.expectedDraftRevision() == null
                || body.expectedDeploymentRevision() == null) throw invalid();
        return agents.publish(
                owner, id, body.expectedDraftRevision(), body.expectedDeploymentRevision());
    }

    @GetMapping("/{id}/history")
    public List<DeploymentChange> history(
            HttpServletRequest request,
            @PathVariable String id,
            @RequestParam(defaultValue = "0") long after,
            @RequestParam(defaultValue = "50") int limit) {
        return agents.history(
                BearerIdentity.requirePermission(request, "agent:manage"), id, after, limit);
    }

    @GetMapping("/{id}/catalog")
    public AgentCapabilityCatalog catalog(HttpServletRequest request, @PathVariable String id) {
        return agents.catalog(BearerIdentity.requirePermission(request, "agent:manage"), id);
    }

    private static Problem invalid() {
        return new Problem(422, "invalid_body");
    }
}
