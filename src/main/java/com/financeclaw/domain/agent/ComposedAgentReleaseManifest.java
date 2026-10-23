package com.financeclaw.domain.agent;

import com.financeclaw.domain.Governance.Blocked;

/** 独立 v3 格式保存已解析能力计划；旧 v1/v2 正文及其摘要算法保持原样。 */
public record ComposedAgentReleaseManifest(
        String format,
        String agentId,
        String tenant,
        String subject,
        long draftRevision,
        String displayName,
        String systemPrompt,
        String baseReleaseHash,
        AgentExecutionPlan plan,
        String runtimeIdentity) {
    public static final String FORMAT = "agent-release/3";
    public static final String RUNTIME_IDENTITY = "agentscope-2.0.3/p2b";

    public static ComposedAgentReleaseManifest from(
            AgentDefinition agent, AgentDraft draft, AgentExecutionPlan plan) {
        return new ComposedAgentReleaseManifest(
                FORMAT,
                agent.id(),
                agent.tenant(),
                agent.subject(),
                draft.revision(),
                draft.displayName(),
                draft.systemPrompt(),
                draft.baseReleaseHash(),
                plan,
                RUNTIME_IDENTITY);
    }

    public void validate() {
        if (!FORMAT.equals(format)
                || !AgentDraft.isCustomId(agentId)
                || tenant == null
                || tenant.isBlank()
                || subject == null
                || subject.isBlank()
                || draftRevision < 1
                || !AgentDraft.validContent(displayName, systemPrompt)
                || !CapabilityBinding.validHash(baseReleaseHash)
                || plan == null) {
            throw new Blocked("agent_manifest_invalid");
        }
        if (!RUNTIME_IDENTITY.equals(runtimeIdentity)) {
            throw new Blocked("agent_runtime_incompatible");
        }
    }
}
