package com.financeclaw.infrastructure.execution;

import com.financeclaw.app.execution.port.ExecutionRelease;
import com.financeclaw.app.release.ReleaseService;
import com.financeclaw.domain.ExecutionPolicy;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.Task.Turn;
import org.springframework.stereotype.Component;

/**
 * 实现 ExecutionRelease，将发布目录投影为执行所需的权限与调用预算。 通过明确 hash 查找固定发布，而非直接读取当前环境；发布缺失或不可执行时由 ReleaseService
 * 阻断。该适配器不解析模型凭据。
 */
@Component
public class CatalogExecutionRelease implements ExecutionRelease {
    private final ReleaseService catalog;

    public CatalogExecutionRelease(ReleaseService catalog) {
        this.catalog = catalog;
    }

    @Override
    public String currentHash() {
        return catalog.currentHash();
    }

    @Override
    public ExecutionPolicy require(String hash) {
        var r = catalog.require(hash);
        return new ExecutionPolicy(r.scopes(), r.maxModelCalls(), r.maxToolCalls());
    }

    @Override
    public ExecutionPolicy requireForTurn(Turn turn) {
        var resolved = catalog.resolve(turn.releaseHash());
        resolved.requireIdentity(new Owner(turn.tenant(), turn.subject()), turn.agentId());
        var r = resolved.definition();
        return new ExecutionPolicy(r.scopes(), r.maxModelCalls(), r.maxToolCalls());
    }
}
