package com.financeclaw.domain.agent;

import com.financeclaw.domain.release.ReleaseDefinition;
import java.util.List;

/** 发布冻结的实际执行计划；definition 是能力收窄后的投影，不作为 legacy 清单重新登记。 */
public record AgentExecutionPlan(
        String modelProfile,
        String runtimeArtifactHash,
        List<String> requestedCapabilities,
        List<CapabilityBinding> capabilities,
        ReleaseDefinition definition) {
    public AgentExecutionPlan {
        requestedCapabilities =
                requestedCapabilities == null ? null : List.copyOf(requestedCapabilities);
        capabilities = capabilities == null ? null : List.copyOf(capabilities);
    }
}
