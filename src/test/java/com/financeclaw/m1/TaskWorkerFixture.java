package com.financeclaw.m1;

import com.financeclaw.app.execution.*;
import com.financeclaw.app.execution.port.AgentExecutionPort;
import com.financeclaw.worker.TaskWorker;
import org.springframework.context.ApplicationContext;

/** Reuses the real task use cases with a scenario-specific native runtime. */
public final class TaskWorkerFixture {
    private TaskWorkerFixture() {}

    public static TaskWorker create(
            ApplicationContext context,
            AgentExecutionPort runtime,
            boolean enabled,
            int parallelism,
            long lease,
            long heartbeat) {
        return new TaskWorker(
                context.getBean(ExecutionLeaseService.class),
                context.getBean(RecoverTaskService.class),
                new ExecuteTaskService(context.getBean(ExecutionCompletionService.class), runtime),
                context.getBean(com.financeclaw.app.task.WorkSignals.class),
                enabled,
                parallelism,
                lease,
                heartbeat);
    }

    public static TaskWorker create(
            LifecycleDatabase db,
            AgentExecutionPort runtime,
            boolean enabled,
            int parallelism,
            long lease,
            long heartbeat) {
        return new TaskWorker(
                db.leases,
                db.recovery,
                new ExecuteTaskService(db.completion, runtime),
                db.signals,
                enabled,
                parallelism,
                lease,
                heartbeat);
    }
}
