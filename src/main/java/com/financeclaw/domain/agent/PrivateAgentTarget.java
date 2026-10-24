package com.financeclaw.domain.agent;

import com.financeclaw.domain.ExecutionScope;
import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.Task.Owner;
import java.util.List;
import java.util.Map;
import java.util.Objects;

/**
 * 父发布中冻结的私有叶子 Agent 目标。模型只提供显式 message，目标身份与发布摘要只能来自已校验的能力绑定。 目标不继承父记忆或工具；运行工件兼容性由基础设施在装配和发布时另外核验。
 */
public record PrivateAgentTarget(String agentId, String releaseHash) {
    public static final String TOOL_SCOPE = "artifact:read";
    public static final String DESCRIPTION =
            "Delegate an explicit message to a release-pinned private leaf Agent.";
    public static final Map<String, Object> INPUT_SCHEMA =
            Map.of(
                    "type",
                    "object",
                    "properties",
                    Map.of("message", Map.of("type", "string", "minLength", 1, "maxLength", 16384)),
                    "required",
                    List.of("message"),
                    "additionalProperties",
                    false);

    public PrivateAgentTarget {
        if (!AgentDraft.isCustomId(agentId) || !CapabilityBinding.validHash(releaseHash)) {
            throw new Blocked("agent_plan_invalid");
        }
    }

    public static String toolName(String id) {
        if (!AgentDraft.isCustomId(id)) throw new Blocked("agent_plan_invalid");
        return "delegate_" + id.replace("-", "");
    }

    /** 校验私有 owner 与显式空计划；调用方仍需将 resolved.agentId 与本目标 agentId 精确比对。 */
    public static void requireLeaf(Owner owner, ResolvedAgentRelease resolved) {
        if (resolved == null || !AgentDraft.isCustomId(resolved.agentId())) {
            throw new Blocked("agent_leaf_required");
        }
        if (owner == null
                || !Objects.equals(owner.tenant(), resolved.tenant())
                || !Objects.equals(owner.subject(), resolved.subject())) {
            throw new Blocked("agent_identity_mismatch");
        }
        var plan = resolved.plan();
        if (plan == null
                || plan.requestedCapabilities() == null
                || !plan.requestedCapabilities().isEmpty()
                || plan.capabilities() == null
                || !plan.capabilities().isEmpty()
                || resolved.definition() == null
                || !resolved.definition().scopes().isEmpty()
                || !Objects.equals(plan.definition(), resolved.definition())) {
            throw new Blocked("agent_leaf_required");
        }
    }

    /** 与 SQL 隐私清理共用同一稳定派生式；父 attempt 的 scope 已包含根命令和历史 attempt 身份。 */
    public ExecutionScope scope(ExecutionScope root, String callId) {
        if (callId == null || callId.isBlank() || callId.length() > 512) {
            throw new Blocked("child_invocation_invalid");
        }
        return new ExecutionScope(
                root.tenant(),
                root.subject(),
                ExecutionScope.hash(
                        List.of(root.key(), "private_agent", callId, agentId, releaseHash)),
                agentId,
                releaseHash);
    }
}
