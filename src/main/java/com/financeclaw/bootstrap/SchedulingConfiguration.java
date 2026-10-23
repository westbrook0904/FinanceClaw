package com.financeclaw.bootstrap;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.scheduling.concurrent.ThreadPoolTaskScheduler;

/**
 * 后台调度器配置：使用有界线程数量和清晰线程名，便于定位阻塞。
 *
 * <p>普通轮询与执行心跳分离，慢恢复或远程调用不得占满续租线程。MVC 请求执行器仍由 Boot 独立配置。
 */
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
