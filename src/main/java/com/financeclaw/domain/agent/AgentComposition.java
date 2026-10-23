package com.financeclaw.domain.agent;

import com.financeclaw.domain.Task.Problem;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

/** 操作员选择的能力引用；空列表明确表示不选择，只有 null 组合沿用旧版继承语义。 */
public record AgentComposition(String modelProfile, List<String> capabilities) {
    public static final int MAX_CAPABILITIES = 128;

    public AgentComposition {
        if (!Set.of("primary", "fallback").contains(modelProfile == null ? "" : modelProfile)
                || capabilities == null
                || capabilities.size() > MAX_CAPABILITIES
                || capabilities.stream().anyMatch(id -> !validId(id))
                || new HashSet<>(capabilities).size() != capabilities.size()) {
            throw new Problem(422, "invalid_agent_composition");
        }
        capabilities = capabilities.stream().sorted().toList();
    }

    public static boolean validId(String id) {
        return id != null
                && id.length() <= 272
                && id.matches(
                        "(?:function|skill):[A-Za-z][A-Za-z0-9_.-]{0,127}|mcp:[A-Za-z][A-Za-z0-9_.-]{0,127}/[A-Za-z][A-Za-z0-9_.-]{0,127}");
    }
}
