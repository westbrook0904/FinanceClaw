package com.financeclaw.app.agent.port;

import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.agent.AgentCapabilityCatalog;
import com.financeclaw.domain.agent.AgentDraft;
import com.financeclaw.domain.agent.AgentExecutionPlan;

/** 受控依赖发现端口；解析可能访问远端，调用方必须在数据库事务外执行。 */
public interface AgentCapabilityResolver {
    AgentExecutionPlan resolve(Owner owner, AgentDraft draft);

    AgentCapabilityCatalog catalog(Owner owner, String baseReleaseHash);
}
