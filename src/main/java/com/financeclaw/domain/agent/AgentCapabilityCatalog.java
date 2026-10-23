package com.financeclaw.domain.agent;

import java.util.List;

/** Agent 固定基础发布可供操作员选择的模型和已安装能力。 */
public record AgentCapabilityCatalog(
        List<String> modelProfiles, List<CapabilityOption> capabilities) {
    public AgentCapabilityCatalog {
        modelProfiles = List.copyOf(modelProfiles);
        capabilities = List.copyOf(capabilities);
    }
}
