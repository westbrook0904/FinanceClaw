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
public record PrivateAgentTarget(String agentId, String releaseHash, Profile profile) {
    /** 闭集执行档案，不接受模型指定任意工具集合。EMPTY 是原 v1 零工具契约。 */
    public enum Profile {
        EMPTY,
        MARKET_SNAPSHOT
    }

    public static final String TOOL_SCOPE = "artifact:read";
    public static final String MARKET_SCOPE = "market:read";
    public static final String SNAPSHOT = "function:market_snapshot";
    public static final String READER = "function:read_artifact_range";
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

    /** 保留旧构造入口的含义；普通路由 DTO 的默认值不构成私有叶子资格证明。 */
    public PrivateAgentTarget(String agentId, String releaseHash) {
        this(agentId, releaseHash, Profile.EMPTY);
    }

    public PrivateAgentTarget {
        if (!AgentDraft.isCustomId(agentId)
                || !CapabilityBinding.validHash(releaseHash)
                || profile == null) {
            throw new Blocked("agent_plan_invalid");
        }
    }

    /** 父委派结果（包括归档文件）必须保留真正的数据来源权限。 */
    public String resultScope() {
        return profile == Profile.MARKET_SNAPSHOT ? MARKET_SCOPE : TOOL_SCOPE;
    }

    public int maxToolCalls() {
        return profile == Profile.MARKET_SNAPSHOT ? 1 : 0;
    }

    public static String toolName(String id) {
        if (!AgentDraft.isCustomId(id)) throw new Blocked("agent_plan_invalid");
        return "delegate_" + id.replace("-", "");
    }

    /** 校验私有 owner 与显式空计划；调用方仍需将 resolved.agentId 与本目标 agentId 精确比对。 */
    public static void requireLeaf(Owner owner, ResolvedAgentRelease resolved) {
        requireLeaf(owner, resolved, Profile.EMPTY);
    }

    /**
     * 校验选中发布的完整收窄形状。目录中的 profile 只是集合查询得到的候选提示，必须再次通过此校验； market 的 reader 仅是发布编译依赖，子运行时仍只能暴露
     * snapshot，不能继承父工具或文件访问。 本方法保持纯领域验证，本地工具契约和运行工件兼容性由装配层另行严格校验。
     */
    public static void requireLeaf(Owner owner, ResolvedAgentRelease resolved, Profile profile) {
        if (resolved == null || !AgentDraft.isCustomId(resolved.agentId())) {
            throw new Blocked("agent_leaf_required");
        }
        if (owner == null
                || !Objects.equals(owner.tenant(), resolved.tenant())
                || !Objects.equals(owner.subject(), resolved.subject())) {
            throw new Blocked("agent_identity_mismatch");
        }
        var plan = resolved.plan();
        if (profile == null
                || plan == null
                || plan.requestedCapabilities() == null
                || plan.capabilities() == null
                || resolved.definition() == null
                || !Objects.equals(plan.definition(), resolved.definition())) {
            throw new Blocked("agent_leaf_required");
        }
        boolean matches =
                switch (profile) {
                    case EMPTY ->
                            plan.requestedCapabilities().isEmpty()
                                    && plan.capabilities().isEmpty()
                                    && resolved.definition().scopes().isEmpty();
                    case MARKET_SNAPSHOT ->
                            plan.requestedCapabilities().equals(List.of(SNAPSHOT))
                                    && marketClosure(plan.capabilities())
                                    && resolved.definition().scopes().equals(List.of(MARKET_SCOPE))
                                    && resolved.definition().maxToolCalls() >= 1;
                };
        if (!matches) throw new Blocked("agent_leaf_required");
    }

    private static boolean marketClosure(List<CapabilityBinding> bindings) {
        if (bindings.size() != 2) return false;
        var snapshot = bindings.get(0);
        var reader = bindings.get(1);
        return snapshot != null
                && reader != null
                && SNAPSHOT.equals(snapshot.id())
                && "function".equals(snapshot.kind())
                && "market_snapshot".equals(snapshot.name())
                && List.of(MARKET_SCOPE).equals(snapshot.scopes())
                && List.of(READER).equals(snapshot.dependencies())
                && READER.equals(reader.id())
                && "function".equals(reader.kind())
                && "read_artifact_range".equals(reader.name())
                && List.of().equals(reader.scopes())
                && List.of().equals(reader.dependencies());
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
