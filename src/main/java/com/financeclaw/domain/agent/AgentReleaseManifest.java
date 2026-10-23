package com.financeclaw.domain.agent;

import com.financeclaw.domain.Governance.Blocked;

/** 新格式发布正文：固定 owner、草稿、提示词和单层 legacy 基础清单，不改写旧发布格式。 */
public record AgentReleaseManifest(
        String format,
        String agentId,
        String tenant,
        String subject,
        long draftRevision,
        String displayName,
        String systemPrompt,
        String baseReleaseHash,
        String runtimeIdentity) {
    public static final String FORMAT = "agent-release/2";
    public static final String RUNTIME_IDENTITY = "agentscope-2.0.3/p2a";

    public static AgentReleaseManifest from(AgentDefinition agent, AgentDraft draft) {
        return new AgentReleaseManifest(
                FORMAT,
                agent.id(),
                agent.tenant(),
                agent.subject(),
                draft.revision(),
                draft.displayName(),
                draft.systemPrompt(),
                draft.baseReleaseHash(),
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
                || baseReleaseHash == null
                || !baseReleaseHash.matches("[0-9a-f]{64}")) {
            throw new Blocked("agent_manifest_invalid");
        }
        if (!RUNTIME_IDENTITY.equals(runtimeIdentity)) {
            throw new Blocked("agent_runtime_incompatible");
        }
    }
}
