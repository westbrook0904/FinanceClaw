package com.financeclaw.api;

import com.financeclaw.app.agent.port.ResourceAdministration;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.Task.Problem;
import com.financeclaw.domain.authorization.ResourceGrant;
import jakarta.servlet.http.HttpServletRequest;
import java.util.List;
import org.springframework.web.bind.annotation.*;

/** 管理资源授权时 actor 永远来自认证目录；正文只选择接收主体，不提供资源 owner。 */
@RestController
@RequestMapping("/v1")
public class ResourceAccessController {
    private final ResourceAdministration access;

    public ResourceAccessController(ResourceAdministration access) {
        this.access = access;
    }

    public record Change(
            String granteeTenant,
            String granteeSubject,
            String releaseHash,
            Long expectedRevision,
            Boolean enabled) {}

    public record SharedAgent(String agentId, String releaseHash, long grantRevision) {}

    public record CredentialResource(String resourceId) {}

    public record GrantView(
            String granteeTenant,
            String granteeSubject,
            String releaseHash,
            long revision,
            boolean enabled) {
        static GrantView from(ResourceGrant g) {
            return new GrantView(
                    g.granteeTenant(),
                    g.granteeSubject(),
                    g.releaseHash(),
                    g.revision(),
                    g.enabled());
        }
    }

    @GetMapping("/agents/shared")
    public List<SharedAgent> shared(HttpServletRequest request) {
        return access.listSharedTargets(TaskController.owner(request)).stream()
                .map(g -> new SharedAgent(g.agentId(), g.releaseHash(), g.grantRevision()))
                .toList();
    }

    @GetMapping("/agents/{id}/shares")
    public List<GrantView> shares(HttpServletRequest request, @PathVariable String id) {
        return access
                .grants(BearerIdentity.requirePermission(request, "agent:share"), "agent", id)
                .stream()
                .map(GrantView::from)
                .toList();
    }

    @PutMapping("/agents/{id}/shares")
    public GrantView share(
            HttpServletRequest request, @PathVariable String id, @RequestBody Change body) {
        return change(BearerIdentity.requirePermission(request, "agent:share"), "agent", id, body);
    }

    @GetMapping("/agents/{id}/shares/history")
    public List<GrantView> shareHistory(
            HttpServletRequest request,
            @PathVariable String id,
            @RequestParam(required = false) String granteeTenant,
            @RequestParam(required = false) String granteeSubject,
            @RequestParam(defaultValue = "0") long after,
            @RequestParam(defaultValue = "50") int limit) {
        return history(
                BearerIdentity.requirePermission(request, "agent:share"),
                "agent",
                id,
                granteeTenant,
                granteeSubject,
                after,
                limit);
    }

    @GetMapping("/credential-resources")
    public List<CredentialResource> credentials(HttpServletRequest request) {
        return access
                .credentialResources(
                        BearerIdentity.requirePermission(request, "credentials:manage"))
                .stream()
                .map(r -> new CredentialResource(r.resourceId()))
                .toList();
    }

    @GetMapping("/credential-resources/{id}/grants")
    public List<GrantView> credentialGrants(HttpServletRequest request, @PathVariable String id) {
        return access
                .grants(
                        BearerIdentity.requirePermission(request, "credentials:manage"),
                        "credential",
                        id)
                .stream()
                .map(GrantView::from)
                .toList();
    }

    @PutMapping("/credential-resources/{id}/grants")
    public GrantView credentialGrant(
            HttpServletRequest request, @PathVariable String id, @RequestBody Change body) {
        return change(
                BearerIdentity.requirePermission(request, "credentials:manage"),
                "credential",
                id,
                body);
    }

    @GetMapping("/credential-resources/{id}/grants/history")
    public List<GrantView> credentialHistory(
            HttpServletRequest request,
            @PathVariable String id,
            @RequestParam(required = false) String granteeTenant,
            @RequestParam(required = false) String granteeSubject,
            @RequestParam(defaultValue = "0") long after,
            @RequestParam(defaultValue = "50") int limit) {
        return history(
                BearerIdentity.requirePermission(request, "credentials:manage"),
                "credential",
                id,
                granteeTenant,
                granteeSubject,
                after,
                limit);
    }

    private GrantView change(Owner owner, String kind, String id, Change body) {
        if (body == null || body.expectedRevision() == null || body.enabled() == null)
            throw new Problem(422, "invalid_resource_grant");
        return GrantView.from(
                access.change(
                        owner,
                        kind,
                        id,
                        grantee(body.granteeTenant(), body.granteeSubject()),
                        body.releaseHash(),
                        body.expectedRevision(),
                        body.enabled()));
    }

    private List<GrantView> history(
            Owner owner,
            String kind,
            String id,
            String tenant,
            String subject,
            long after,
            int limit) {
        return access.history(owner, kind, id, grantee(tenant, subject), after, limit).stream()
                .map(GrantView::from)
                .toList();
    }

    private static Owner grantee(String tenant, String subject) {
        if (!TrustedIdentityDirectory.validIdentity(tenant)
                || !TrustedIdentityDirectory.validIdentity(subject))
            throw new Problem(422, "invalid_resource_grant");
        return new Owner(tenant, subject);
    }
}
