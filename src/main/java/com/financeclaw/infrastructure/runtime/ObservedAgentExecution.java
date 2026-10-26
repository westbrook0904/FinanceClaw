package com.financeclaw.infrastructure.runtime;

import com.financeclaw.app.execution.port.AgentExecutionPort;
import com.financeclaw.domain.Governance.Blocked;
import com.financeclaw.domain.Task.*;
import io.micrometer.core.instrument.MeterRegistry;
import io.micrometer.core.instrument.Timer;
import java.util.Map;
import java.util.concurrent.TimeUnit;

/** Native调用时长，不把SDK返回等同于业务提交；标签只有固定结果枚举。 */
public final class ObservedAgentExecution implements AgentExecutionPort {
    private final AgentExecutionPort delegate;
    private final RuntimeObservations observations;
    private final Map<String, Timer> timers;

    public ObservedAgentExecution(AgentExecutionPort delegate, MeterRegistry metrics) {
        this(delegate, metrics, RuntimeObservations.disabled());
    }

    public ObservedAgentExecution(
            AgentExecutionPort delegate, MeterRegistry metrics, RuntimeObservations observations) {
        this.delegate = delegate;
        this.observations = observations;
        var map = new java.util.HashMap<String, Timer>();
        for (String result : java.util.List.of("returned", "blocked", "fenced", "error"))
            map.put(
                    result,
                    Timer.builder("financeclaw.runtime.invocation")
                            .description(
                                    "Native invocation elapsed time; returned does not prove business commit")
                            .tag("outcome", result)
                            .register(metrics));
        timers = Map.copyOf(map);
    }

    @Override
    public Outcome execute(Claim claim) {
        return observe(claim, () -> delegate.execute(claim));
    }

    @Override
    public Outcome execute(Claim claim, java.util.function.BooleanSupplier allowed) {
        return observe(claim, () -> delegate.execute(claim, allowed));
    }

    private Outcome observe(Claim claim, java.util.function.Supplier<Outcome> execution) {
        long started = System.nanoTime();
        String result = "error";
        try {
            Outcome outcome = observations.attempt(claim, execution);
            result = "returned";
            return outcome;
        } catch (Blocked blocked) {
            result = "blocked";
            throw blocked;
        } catch (Fenced fenced) {
            result = "fenced";
            throw fenced;
        } finally {
            try {
                timers.get(result).record(System.nanoTime() - started, TimeUnit.NANOSECONDS);
            } catch (Throwable ignored) {
                /* Observation cannot affect a completed call. */
            }
        }
    }

    @Override
    public void interrupt(Claim claim) {
        delegate.interrupt(claim);
    }
}
