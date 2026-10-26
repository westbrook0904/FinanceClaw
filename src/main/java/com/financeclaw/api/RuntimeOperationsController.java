package com.financeclaw.api;

import com.financeclaw.app.runtime.RuntimeArtifactCatalog;
import com.financeclaw.app.runtime.WorkerRegistry;
import com.financeclaw.app.runtime.port.RuntimeUsageGateway;
import com.financeclaw.domain.runtime.*;
import jakarta.servlet.http.HttpServletRequest;
import java.util.List;
import org.springframework.web.bind.annotation.*;

/** 平台运行资源操作，仅显式 runtime:manage 权限可见；不提供远程执行/下载/任意SQL入口。 */
@RestController
@RequestMapping("/v1/runtime")
public final class RuntimeOperationsController {
    private final RuntimeArtifactCatalog artifacts;
    private final WorkerRegistry workers;
    private final RuntimeUsageGateway usage;

    public RuntimeOperationsController(
            RuntimeArtifactCatalog artifacts, WorkerRegistry workers, RuntimeUsageGateway usage) {
        this.artifacts = artifacts;
        this.workers = workers;
        this.usage = usage;
    }

    public record RegisterArtifact(
            String relativePath, String sha256, int protocolVersion, List<String> capabilities) {}

    @PostMapping("/artifacts")
    public RuntimeArtifact register(
            HttpServletRequest request, @RequestBody RegisterArtifact body) {
        BearerIdentity.requirePermission(request, "runtime:manage");
        return artifacts.register(
                body.relativePath(), body.sha256(), body.protocolVersion(), body.capabilities());
    }

    @GetMapping("/artifacts")
    public List<RuntimeArtifact> artifacts(
            HttpServletRequest request, @RequestParam(defaultValue = "50") int limit) {
        BearerIdentity.requirePermission(request, "runtime:manage");
        return artifacts.list(limit);
    }

    @PostMapping("/artifacts/{hash}/verify")
    public RuntimeArtifact verify(HttpServletRequest request, @PathVariable String hash) {
        BearerIdentity.requirePermission(request, "runtime:manage");
        return artifacts.verify(hash);
    }

    @GetMapping("/workers")
    public List<RuntimeWorker> workers(
            HttpServletRequest request, @RequestParam(defaultValue = "50") int limit) {
        BearerIdentity.requirePermission(request, "runtime:manage");
        return workers.list(limit);
    }

    @GetMapping("/usage")
    public RuntimeUsage usage(HttpServletRequest request) {
        BearerIdentity.requirePermission(request, "runtime:manage");
        return usage.snapshot();
    }
}
