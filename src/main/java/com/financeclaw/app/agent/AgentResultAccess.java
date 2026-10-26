package com.financeclaw.app.agent;

import com.financeclaw.app.agent.port.AgentAccessGateway;
import com.financeclaw.app.release.ReleaseService;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.support.PrivateAgentContracts;

/** A conservative source boundary until individual artifacts/events carry source provenance. */
public final class AgentResultAccess {
    private AgentResultAccess() {}

    public static com.financeclaw.domain.agent.ResolvedAgentRelease require(
            AgentAccessGateway agents,
            ReleaseService releases,
            Owner caller,
            String agentId,
            String releaseHash) {
        var authorization = agents.requireInvocation(caller, agentId, releaseHash);
        var resolved =
                authorization.resolvedRelease() == null
                        ? releases.resolve(releaseHash)
                        : authorization.resolvedRelease();
        resolved.requireIdentity(authorization.resourceOwner(), agentId);
        if (resolved.plan() == null) return resolved;
        for (var binding : resolved.plan().capabilities()) {
            if (!binding.kind().equals("agent")) continue;
            var target = PrivateAgentContracts.from(binding);
            var child = agents.requireInvocation(caller, target.agentId(), target.releaseHash());
            var childRelease =
                    child.resolvedRelease() == null
                            ? releases.resolve(target.releaseHash())
                            : child.resolvedRelease();
            childRelease.requireIdentity(child.resourceOwner(), target.agentId());
        }
        return resolved;
    }
}
