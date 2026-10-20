package com.financeclaw.worker;

import com.financeclaw.app.task.ProjectTaskService;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

/** Integrations consumes durable projection intents without constructing an agent executor. */
@Component
public class OutboxProjector {
  private final ProjectTaskService projection;
  private final boolean enabled;

  public OutboxProjector(
      ProjectTaskService projection,
      @Value("${financeclaw.integrations.enabled:true}") boolean enabled) {
    this.projection = projection;
    this.enabled = enabled;
  }

  @Scheduled(fixedDelayString = "${financeclaw.worker.poll-ms:250}")
  public void poll() {
    if (enabled)
      for (int i = 0; i < 50 && projection.projectNext(); i++) {
        /* Preserve the existing bounded local outbox drain. */
      }
  }
}
