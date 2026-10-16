package com.financeclaw.m1;

import com.financeclaw.app.execution.*;
import com.financeclaw.app.execution.port.AgentExecutionPort;
import com.financeclaw.app.task.ProjectTaskService;
import com.financeclaw.worker.TaskWorker;
import org.springframework.context.ApplicationContext;

/** Reuses the real task use cases with a scenario-specific native runtime. */
public final class TaskWorkerFixture {
  private TaskWorkerFixture() {}

  public static TaskWorker create(
      ApplicationContext context,
      AgentExecutionPort runtime,
      boolean enabled,
      boolean integrations,
      int parallelism,
      long lease,
      long heartbeat) {
    return new TaskWorker(
        context.getBean(ExecutionLeaseService.class),
        context.getBean(RecoverTaskService.class),
        context.getBean(ProjectTaskService.class),
        new ExecuteTaskService(context.getBean(ExecutionCompletionService.class), runtime),
        enabled,
        integrations,
        parallelism,
        lease,
        heartbeat);
  }

  public static TaskWorker create(
      LifecycleDatabase db,
      AgentExecutionPort runtime,
      boolean enabled,
      boolean integrations,
      int parallelism,
      long lease,
      long heartbeat) {
    return new TaskWorker(
        db.leases,
        db.recovery,
        db.projection,
        new ExecuteTaskService(db.completion, runtime),
        enabled,
        integrations,
        parallelism,
        lease,
        heartbeat);
  }
}
