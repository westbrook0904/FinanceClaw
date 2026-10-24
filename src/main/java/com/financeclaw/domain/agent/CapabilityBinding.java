package com.financeclaw.domain.agent;

import com.financeclaw.domain.Governance.Blocked;
import java.nio.charset.StandardCharsets;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

/** 已解析的单个能力契约；正文及其摘要一起进入发布，依赖只能引用同一计划中的能力。 */
public record CapabilityBinding(
        String id,
        String kind,
        String name,
        List<String> scopes,
        List<String> dependencies,
        String contractJson,
        String contentHash) {
    public static final int MAX_CONTRACT_BYTES = 262_144;

    public CapabilityBinding {
        scopes = scopes == null ? null : List.copyOf(scopes);
        dependencies = dependencies == null ? null : List.copyOf(dependencies);
    }

    public void validate() {
        if (!AgentComposition.validId(id)
                || kind == null
                || !Set.of("function", "skill", "mcp", "agent").contains(kind)
                || !id.startsWith(kind + ":")
                || name == null
                || !name.matches("[A-Za-z][A-Za-z0-9_.-]{0,127}")
                || !(kind.equals("mcp")
                        ? id.endsWith("/" + name)
                        : kind.equals("agent")
                                ? name.equals(PrivateAgentTarget.toolName(id.substring(6)))
                                : id.equals(kind + ":" + name))
                || scopes == null
                || scopes.size() > 32
                || scopes.stream().anyMatch(s -> s == null || s.isBlank() || s.length() > 128)
                || new HashSet<>(scopes).size() != scopes.size()
                || dependencies == null
                || dependencies.size() > AgentComposition.MAX_CAPABILITIES
                || dependencies.stream().anyMatch(d -> !AgentComposition.validId(d))
                || new HashSet<>(dependencies).size() != dependencies.size()
                || contractJson == null
                || contractJson.isBlank()
                || contractJson.indexOf('\0') >= 0
                || !StandardCharsets.UTF_8.newEncoder().canEncode(contractJson)
                || contractJson.getBytes(StandardCharsets.UTF_8).length > MAX_CONTRACT_BYTES
                || !validHash(contentHash)) {
            throw new Blocked("agent_plan_invalid");
        }
    }

    public static boolean validHash(String hash) {
        return hash != null && hash.matches("[0-9a-f]{64}");
    }
}
