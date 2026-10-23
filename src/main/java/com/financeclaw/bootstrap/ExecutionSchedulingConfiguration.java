package com.financeclaw.bootstrap;

import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.scheduling.concurrent.ThreadPoolTaskScheduler;

/**
 * 执行心跳专用调度器，隔离普通轮询与恢复任务的调度阻塞。
 *
 * <p>该调度器不创建独立数据库连接池；续租仍受统一连接池、语句超时和租约 token 校验约束。
 */
/** Loaded only in roles that execute tasks. */
@Configuration(proxyBeanMethods = false)
class ExecutionSchedulingConfiguration {
    @Bean
    ThreadPoolTaskScheduler executionHeartbeatScheduler() {
        return SchedulingConfiguration.scheduler(1, "execution-heartbeat-");
    }
}
