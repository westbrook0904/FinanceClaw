package com.financeclaw.app.agent.port;

import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.agent.ResolvedAgentRelease;
import java.util.List;

/** 当前资源授权事实；caller 保持业务数据主体，resourceOwner 只用于核对不可变发布身份。 */
public interface AgentAccessGateway {
    /** 发布仅复用本次授权调用的不可变解析结果；每次调用仍须重新读取当前授权事实。 */
    record Authorization(
            Owner resourceOwner, long grantRevision, ResolvedAgentRelease resolvedRelease) {
        public Authorization(Owner resourceOwner, long grantRevision) {
            this(resourceOwner, grantRevision, null);
        }
    }

    record SharedTarget(
            String agentId, String releaseHash, Owner resourceOwner, long grantRevision) {}

    Authorization requireInvocation(Owner caller, String agentId, String releaseHash);

    Owner resourceOwner(String agentId);

    List<SharedTarget> listSharedTargets(Owner caller);

    /** 旧显式 fixture 保持原 same-owner 契约；调用方仍必须执行 release.requireIdentity。 */
    static AgentAccessGateway ownedOnly() {
        return new AgentAccessGateway() {
            public Authorization requireInvocation(Owner caller, String id, String hash) {
                if (caller == null || id == null || id.isBlank() || hash == null || hash.isBlank())
                    throw new Blocked("agent_access_denied");
                return new Authorization(caller, 0);
            }

            public Owner resourceOwner(String id) {
                throw new Blocked("agent_access_denied");
            }

            public List<SharedTarget> listSharedTargets(Owner caller) {
                return List.of();
            }
        };
    }
}
