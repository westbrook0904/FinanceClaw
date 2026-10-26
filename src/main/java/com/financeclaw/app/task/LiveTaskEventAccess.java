package com.financeclaw.app.task;

import com.financeclaw.app.agent.port.AgentAccessGateway;
import com.financeclaw.app.execution.port.ExecutionMemory;
import com.financeclaw.app.execution.port.ExecutionPolicyGateway;
import com.financeclaw.app.task.port.TaskEventAccess;
import com.financeclaw.app.task.port.TaskQueryGateway;
import com.financeclaw.domain.Task.*;
import org.springframework.transaction.support.TransactionTemplate;

/** SSE 的实时读取门禁。只沿用任务身份与隐私/授权权威，不将本地通知作为授权事实。 */
public final class LiveTaskEventAccess implements TaskEventAccess {
    private final TaskQueryGateway tasks;
    private final ExecutionPolicyGateway governance;
    private final ExecutionMemory memory;
    private final TransactionTemplate tx;
    private final AgentAccessGateway agents;
    private final com.financeclaw.app.release.ReleaseService releases;

    public LiveTaskEventAccess(
            TaskQueryGateway tasks,
            ExecutionPolicyGateway governance,
            ExecutionMemory memory,
            TransactionTemplate tx,
            AgentAccessGateway agents) {
        this(tasks, governance, memory, tx, agents, null);
    }

    public LiveTaskEventAccess(
            TaskQueryGateway tasks,
            ExecutionPolicyGateway governance,
            ExecutionMemory memory,
            TransactionTemplate tx,
            AgentAccessGateway agents,
            com.financeclaw.app.release.ReleaseService releases) {
        this.releases = releases;
        this.tasks = tasks;
        this.governance = governance;
        this.memory = memory;
        this.tx = tx;
        this.agents = agents;
    }

    @Override
    public void check(Turn subscribed) {
        tx.executeWithoutResult(
                status -> {
                    var current =
                            tasks.ownedTurn(
                                    new Owner(subscribed.tenant(), subscribed.subject()),
                                    subscribed.conversationId(),
                                    subscribed.id());
                    if (current == null
                            || !current.conversationId().equals(subscribed.conversationId()))
                        throw new Problem(404, "not_found");
                    // 与执行门禁保持隐私 owner → grant 的锁序；事务结束后才把帧交给网络。
                    memory.check(current);
                    if (releases == null)
                        agents.requireInvocation(
                                new Owner(current.tenant(), current.subject()),
                                current.agentId(),
                                current.releaseHash());
                    else
                        com.financeclaw.app.agent.AgentResultAccess.require(
                                agents,
                                releases,
                                new Owner(current.tenant(), current.subject()),
                                current.agentId(),
                                current.releaseHash());
                    var policy = governance.policy(current.id());
                    var grant = governance.lockGrant(current.conversationId());
                    if (policy == null
                            || grant == null
                            || policy.grantRevision() != grant.revision())
                        throw new Problem(403, "authorization_changed");
                });
    }
}
