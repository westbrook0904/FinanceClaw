package com.financeclaw.bootstrap;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.scheduling.concurrent.ThreadPoolTaskScheduler;

/** Heartbeats must not queue behind notification delivery or expired-execution recovery. */
@Configuration(proxyBeanMethods = false)
class SchedulingConfiguration {
  @Bean
  ThreadPoolTaskScheduler taskScheduler(
      @Value("${spring.task.scheduling.pool.size:2}") int poolSize) {
    return scheduler(poolSize, "background-");
  }

  static ThreadPoolTaskScheduler scheduler(int poolSize, String prefix) {
    var scheduler = new ThreadPoolTaskScheduler();
    scheduler.setPoolSize(poolSize);
    scheduler.setThreadNamePrefix(prefix);
    scheduler.setRemoveOnCancelPolicy(true);
    return scheduler;
  }
}
