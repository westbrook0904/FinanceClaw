package com.financeclaw.domain.agent;

/** Agent 的稳定身份与草稿指针；owner 不随编辑或发布变化，内置虚拟定义的 owner 为空。 */
public record AgentDefinition(String id, String tenant, String subject, long draftRevision) {}
