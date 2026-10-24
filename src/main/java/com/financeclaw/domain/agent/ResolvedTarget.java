package com.financeclaw.domain.agent;

/** 受理事务解析后的调用目标。普通路由默认 EMPTY 仅保持旧 DTO 含义，不能作为叶子资格证明； 私有目录显式返回候选 profile，选中后仍须解析不可变清单并核验精确计划。 */
public record ResolvedTarget(
        String agentId, String releaseHash, PrivateAgentTarget.Profile profile) {
    public ResolvedTarget(String agentId, String releaseHash) {
        this(agentId, releaseHash, PrivateAgentTarget.Profile.EMPTY);
    }
}
