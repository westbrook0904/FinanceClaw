package com.financeclaw.domain.agent;

/** 受理事务解析后的调用目标；后续执行只使用保存的 Agent 与 release hash。 */
public record ResolvedTarget(String agentId, String releaseHash) {}
