package com.financeclaw.bootstrap;

import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.scheduling.concurrent.ThreadPoolTaskScheduler;

/** Loaded only in roles that execute tasks. */
@Configuration(proxyBeanMethods = false)
class ExecutionSchedulingConfiguration {
  @Bean
  ThreadPoolTaskScheduler executionHeartbeatScheduler() {
    return SchedulingConfiguration.scheduler(1, "execution-heartbeat-");
  }
}
