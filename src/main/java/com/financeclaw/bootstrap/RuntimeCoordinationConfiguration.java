package com.financeclaw.bootstrap;

import com.financeclaw.api.RuntimeOperationsController;
import com.financeclaw.app.agent.port.AgentAccessGateway;
import com.financeclaw.app.execution.port.*;
import com.financeclaw.app.release.ReleaseService;
import com.financeclaw.app.runtime.*;
import com.financeclaw.app.runtime.port.*;
import com.financeclaw.app.task.LiveTaskEventAccess;
import com.financeclaw.app.task.PersistentEventSignals;
import com.financeclaw.app.task.port.*;
import com.financeclaw.infrastructure.agentscope.RuntimeArtifacts;
import com.financeclaw.infrastructure.agentscope.execution.TaskAgentRuntime;
import com.financeclaw.infrastructure.runtime.*;
import io.micrometer.core.instrument.MeterRegistry;
import java.math.BigDecimal;
import javax.sql.DataSource;
import org.springframework.context.annotation.*;
import org.springframework.core.env.Environment;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.transaction.support.TransactionTemplate;

/** 仅在业务运行入口装配；受限Memory/恢复/初始化/管理CLI不导入。 */
@Configuration(proxyBeanMethods = false)
@Import({
    RuntimeOperationsController.class,
    ObservationConfiguration.class,
    WorkerReadinessHealth.class
})
public class RuntimeCoordinationConfiguration {
    @Bean
    TaskEventAccess taskEventAccess(
            TaskQueryGateway tasks,
            ExecutionPolicyGateway governance,
            ExecutionMemory memory,
            TransactionTemplate tx,
            AgentAccessGateway agents,
            ReleaseService releases) {
        return new LiveTaskEventAccess(tasks, governance, memory, tx, agents, releases);
    }

    @Bean
    PersistentEventSignals persistentEventSignals() {
        return new PersistentEventSignals();
    }

    @Bean(initMethod = "start", destroyMethod = "close")
    PostgresEventListener persistentEventListener(
            DataSource source, PersistentEventSignals signals, Environment env)
            throws java.sql.SQLException {
        // 预算已由 heartbeat reservation 划分；常驻 LISTEN 后至少留一条业务连接。
        if (source.unwrap(com.zaxxer.hikari.HikariDataSource.class).getMaximumPoolSize() < 2)
            throw new IllegalArgumentException(
                    "persistent_events_require_two_business_connections");
        return new PostgresEventListener(
                source,
                signals,
                env.getProperty(
                        "financeclaw.sse.notification-receive-timeout-ms", Long.class, 1000L),
                env.getProperty("financeclaw.sse.notification-reconnect-ms", Long.class, 1000L));
    }

    @Bean
    WorkerRegistry workerRegistry(JdbcTemplate sql, Environment env) {
        long ttl = env.getProperty("financeclaw.runtime.worker-ttl-ms", Long.class, 15000L);
        long poll = env.getProperty("financeclaw.runtime.control-poll-ms", Long.class, 1000L);
        if (poll < 100 || poll > 30000 || ttl < poll * 3)
            throw new IllegalArgumentException("invalid_worker_control_intervals");
        // 复用 Boot 托管模板，不在适配器中另建忽略 spring.jdbc.template.* 的实例。
        // 独占 LISTEN 连接与专用心跳资源有各自生命周期，不能并入这个共享模板。
        return new WorkerRegistry(new JdbcWorkerRegistry(sql), RuntimeArtifacts.hash(), ttl);
    }

    @Bean
    RuntimeArtifactCatalog runtimeArtifactCatalog(JdbcTemplate sql, Environment env) {
        return new RuntimeArtifactCatalog(
                new LocalRuntimeArtifactGateway(
                        sql, env.getProperty("financeclaw.runtime.artifact-directory", "")));
    }

    @Bean
    RuntimeUsageGateway runtimeUsage(JdbcTemplate sql, Environment env) {
        return new JdbcRuntimeUsage(
                sql,
                env.getProperty(
                        "financeclaw.runtime.model-call-cost", BigDecimal.class, BigDecimal.ZERO),
                env.getProperty(
                        "financeclaw.runtime.tool-call-cost", BigDecimal.class, BigDecimal.ZERO));
    }

    @Bean
    @Primary
    AgentExecutionPort observedAgentExecution(
            TaskAgentRuntime runtime, MeterRegistry metrics, RuntimeObservations observations) {
        return new ObservedAgentExecution(runtime, metrics, observations);
    }
}
