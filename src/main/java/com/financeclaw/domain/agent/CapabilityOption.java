package com.financeclaw.domain.agent;

import java.util.List;

/** 可选能力的公开目录元数据，不携带契约正文、凭据或提示词。 */
public record CapabilityOption(
        String id, String kind, String name, List<String> scopes, List<String> dependencies) {
    public CapabilityOption {
        scopes = List.copyOf(scopes);
        dependencies = List.copyOf(dependencies);
    }
}
