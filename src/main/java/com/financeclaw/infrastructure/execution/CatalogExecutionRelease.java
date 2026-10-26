package com.financeclaw.infrastructure.execution;

import com.financeclaw.app.agent.port.AgentAccessGateway;
import com.financeclaw.app.agent.port.CredentialAccessGateway;
import com.financeclaw.app.execution.port.ExecutionRelease;
import com.financeclaw.app.release.ReleaseService;
import com.financeclaw.domain.ExecutionPolicy;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.Task.Turn;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Component;

/**
 * 实现 ExecutionRelease，将发布目录投影为执行所需的权限与调用预算。 通过明确 hash 查找固定发布，而非直接读取当前环境；发布缺失或不可执行时由 ReleaseService
 * 阻断。该适配器不解析模型凭据。
 */
@Component
public class CatalogExecutionRelease implements ExecutionRelease {
    private AgentAccessGateway agentAccess = AgentAccessGateway.ownedOnly();
    private CredentialAccessGateway credentialAccess = CredentialAccessGateway.trustedFixture();

    @Autowired
    public void configureCredentialAccess(CredentialAccessGateway access) {
        this.credentialAccess = access;
    }

    @Autowired
    public void configureAgentAccess(AgentAccessGateway access) {
        this.agentAccess = access;
    }

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
        var caller = new Owner(turn.tenant(), turn.subject());
        // Old context may contain a selected child's derived result, even if this model call
        // does not delegate again. Apply the same conservative source gate before any dispatch.
        var resolved =
                com.financeclaw.app.agent.AgentResultAccess.require(
                        agentAccess, catalog, caller, turn.agentId(), turn.releaseHash());
        if (!resolved.definition().primary().name().equals("offline")
                || resolved.definition().fallback() != null
                        && !resolved.definition().fallback().name().equals("offline"))
            credentialAccess.requireModel(caller);
        var r = resolved.definition();
        return new ExecutionPolicy(r.scopes(), r.maxModelCalls(), r.maxToolCalls());
    }
}
