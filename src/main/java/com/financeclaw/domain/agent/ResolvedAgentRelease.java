package com.financeclaw.domain.agent;

import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.release.ReleaseDefinition;

/** 已通过清单和基础发布摘要校验的执行配置；数据面仍需核对当前 owner 与绑定目标。 */
public record ResolvedAgentRelease(
        String agentId,
        String systemPrompt,
        ReleaseDefinition definition,
        String tenant,
        String subject) {
    public void requireIdentity(Owner owner, String expectedAgentId) {
        if (owner == null
                || !agentId.equals(expectedAgentId)
                || (tenant != null
                        && (!tenant.equals(owner.tenant()) || !subject.equals(owner.subject())))) {
            throw new Blocked("agent_identity_mismatch");
        }
    }
}
