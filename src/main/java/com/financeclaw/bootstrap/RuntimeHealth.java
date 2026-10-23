package com.financeclaw.bootstrap;

import com.financeclaw.infrastructure.task.TaskMapper;
import org.springframework.boot.actuate.health.Health;
import org.springframework.boot.actuate.health.HealthIndicator;
import org.springframework.stereotype.Component;

@Component("runtimeStoreHealthIndicator")
public class RuntimeHealth implements HealthIndicator {
  private final TaskMapper db;

  public RuntimeHealth(TaskMapper db) {
    this.db = db;
  }

  @Override
  public Health health() {
    try {
      db.isRuntimeStateTableReadable();
      db.isArtifactTableReadable();
      db.isAdmissionTableReadable();
      db.listExpiredCommandIds();
      return Health.up().build();
    } catch (RuntimeException e) {
      return Health.down().build();
    }
  }
}
