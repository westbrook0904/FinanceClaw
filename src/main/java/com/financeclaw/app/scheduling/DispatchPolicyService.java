package com.financeclaw.app.scheduling;

import com.financeclaw.app.scheduling.port.DispatchPolicies;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.Task.Problem;
import com.financeclaw.domain.scheduling.ConcurrencyLimit;
import com.financeclaw.domain.scheduling.DispatchPolicy;
import org.springframework.transaction.support.TransactionTemplate;

/** 修改持久派发配额与优先级，不直接领取或执行任务。配额适配器与 claim 共享事务锁， 修订 CAS、更新和读回使用同一短事务，避免并发领取按过时限额放行。 */
public final class DispatchPolicyService {
    private final DispatchPolicies db;
    private final TransactionTemplate tx;

    public DispatchPolicyService(DispatchPolicies db, TransactionTemplate tx) {
        this.db = db;
        this.tx = tx;
    }

    public DispatchPolicy get(Owner owner) {
        return db.get(owner);
    }

    public DispatchPolicy update(Owner owner, DispatchPolicy policy) {
        if (policy == null) throw new Problem(422, "invalid_dispatch_policy");
        policy.validate();
        return tx.execute(s -> db.update(owner, policy));
    }

    /** 只调整调用者所属待领取任务的排序；不会重建 Command 或改变已经运行的 attempt。 */
    public void prioritize(Owner owner, String turnId, int priority) {
        if (turnId == null || turnId.length() > 128 || priority < -100 || priority > 100)
            throw new Problem(422, "invalid_priority");
        tx.executeWithoutResult(s -> db.prioritize(owner, turnId, priority));
    }

    public ConcurrencyLimit tenant(Owner owner) {
        return db.tenant(owner.tenant());
    }

    public ConcurrencyLimit updateTenant(Owner owner, ConcurrencyLimit limit) {
        if (limit == null) throw new Problem(422, "invalid_concurrency_limit");
        limit.validate();
        return tx.execute(s -> db.updateTenant(owner.tenant(), limit));
    }

    public ConcurrencyLimit agent(Owner owner, String agent) {
        requireAgentId(agent);
        return db.agent(owner, agent);
    }

    /** 这是同租户同 Agent 跨 subject 的总上限，区别于 owner 配额中的 maximumPerAgent。 */
    public ConcurrencyLimit updateAgent(Owner owner, String agent, ConcurrencyLimit limit) {
        requireAgentId(agent);
        if (limit == null) throw new Problem(422, "invalid_concurrency_limit");
        limit.validate();
        return tx.execute(s -> db.updateAgent(owner, agent, limit));
    }

    private static void requireAgentId(String id) {
        if (!"finance_agent".equals(id) && !com.financeclaw.domain.agent.AgentDraft.isCustomId(id))
            throw new Problem(422, "invalid_agent_id");
    }
}
