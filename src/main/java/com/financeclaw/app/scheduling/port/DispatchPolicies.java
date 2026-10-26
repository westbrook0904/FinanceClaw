package com.financeclaw.app.scheduling.port;

import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.scheduling.ConcurrencyLimit;
import com.financeclaw.domain.scheduling.DispatchPolicy;

public interface DispatchPolicies {
    DispatchPolicy get(Owner owner);

    DispatchPolicy update(Owner owner, DispatchPolicy policy);

    void prioritize(Owner owner, String turnId, int priority);

    ConcurrencyLimit tenant(String tenant);

    ConcurrencyLimit updateTenant(String tenant, ConcurrencyLimit limit);

    ConcurrencyLimit agent(Owner owner, String agent);

    ConcurrencyLimit updateAgent(Owner owner, String agent, ConcurrencyLimit limit);
}
