package com.financeclaw.support;

import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.agent.CapabilityBinding;
import com.financeclaw.domain.agent.PrivateAgentTarget;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** 私有叶子目标的规范契约编码及摘要校验；JSON 细节不进入纯领域对象，也不引入应用服务循环依赖。 */
public final class PrivateAgentContracts {
    private static final String READER = "function:read_artifact_range";

    private PrivateAgentContracts() {}

    public static CapabilityBinding binding(String id, String hash) {
        return binding(new PrivateAgentTarget(id, hash));
    }

    /** 完整契约参与父发布摘要；归档读取仅是父侧依赖，不向零工具子 Agent 授权。 */
    public static CapabilityBinding binding(PrivateAgentTarget target) {
        if (target == null) throw new Blocked("agent_plan_invalid");
        // EMPTY 不增加任何新字段，保持已冻结 v1 契约字节与摘要完全不变。
        var fields =
                new LinkedHashMap<String, Object>(
                        Map.of(
                                "format",
                                "agent-leaf/1",
                                "agentId",
                                target.agentId(),
                                "releaseHash",
                                target.releaseHash(),
                                "description",
                                PrivateAgentTarget.DESCRIPTION,
                                "inputSchema",
                                PrivateAgentTarget.INPUT_SCHEMA,
                                "approval",
                                false,
                                "maxDepth",
                                1,
                                "maxModelCalls",
                                4,
                                "maxToolCalls",
                                0));
        boolean market = target.profile() == PrivateAgentTarget.Profile.MARKET_SNAPSHOT;
        if (market) {
            fields.put("format", "agent-leaf/2");
            fields.put("profile", "market-snapshot-only");
            fields.put("maxToolCalls", 1);
            fields.put("childTools", List.of("market_snapshot"));
            fields.put("toolScopes", List.of(PrivateAgentTarget.MARKET_SCOPE));
            fields.put("resultScope", PrivateAgentTarget.MARKET_SCOPE);
        }
        String contract = PayloadJson.canonical(fields);
        return new CapabilityBinding(
                "agent:" + target.agentId(),
                "agent",
                PrivateAgentTarget.toolName(target.agentId()),
                market
                        ? List.of(PrivateAgentTarget.TOOL_SCOPE, PrivateAgentTarget.MARKET_SCOPE)
                        : List.of(PrivateAgentTarget.TOOL_SCOPE),
                List.of(READER),
                contract,
                PayloadDigest.sha256(contract));
    }

    /** 不信任持久化契约，完整匹配规范字段、依赖、目标和摘要。这里只核验结构，不读取当前发布指针， 不调用其他应用模块，也不递归解析目标发布。 */
    public static PrivateAgentTarget from(CapabilityBinding binding) {
        try {
            if (binding == null
                    || !"agent".equals(binding.kind())
                    || binding.id() == null
                    || !binding.id().startsWith("agent:")) {
                throw new Blocked("agent_plan_invalid");
            }
            var contract = PayloadJson.object(binding.contractJson());
            var hash = contract.get("releaseHash");
            if (!(hash instanceof String)) throw new Blocked("agent_plan_invalid");
            PrivateAgentTarget.Profile profile;
            if ("agent-leaf/1".equals(contract.get("format"))) {
                profile = PrivateAgentTarget.Profile.EMPTY;
            } else if ("agent-leaf/2".equals(contract.get("format"))
                    && "market-snapshot-only".equals(contract.get("profile"))) {
                profile = PrivateAgentTarget.Profile.MARKET_SNAPSHOT;
            } else {
                throw new Blocked("agent_plan_invalid");
            }
            var target = new PrivateAgentTarget(binding.id().substring(6), (String) hash, profile);
            if (!binding(target).equals(binding)) throw new Blocked("agent_plan_invalid");
            return target;
        } catch (RuntimeException invalid) {
            throw new Blocked("agent_plan_invalid");
        }
    }
}
