package com.financeclaw.bootstrap;

import com.financeclaw.api.DispatchController;
import com.financeclaw.api.ScheduleController;
import com.financeclaw.app.agent.AgentService;
import com.financeclaw.app.execution.port.CommandDispatch;
import com.financeclaw.app.scheduling.DispatchPolicyService;
import com.financeclaw.app.scheduling.TriggerService;
import com.financeclaw.app.task.SubmitTaskService;
import com.financeclaw.infrastructure.scheduling.JdbcCommandDispatch;
import com.financeclaw.infrastructure.scheduling.JdbcTriggerRepository;
import com.financeclaw.infrastructure.scheduling.SpringTriggerCadence;
import java.util.concurrent.atomic.AtomicBoolean;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.context.annotation.Import;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.transaction.support.TransactionTemplate;

@Configuration(proxyBeanMethods = false)
@Import({ScheduleController.class, DispatchController.class})
public class TriggerSchedulingConfiguration {
    @Bean
    JdbcTriggerRepository triggerRepository(JdbcTemplate jdbc) {
        return new JdbcTriggerRepository(jdbc);
    }

    @Bean
    JdbcCommandDispatch dispatchPolicies(JdbcTemplate jdbc) {
        return new JdbcCommandDispatch(jdbc);
    }

    @Bean
    DispatchPolicyService dispatchPolicyService(JdbcCommandDispatch db, TransactionTemplate tx) {
        return new DispatchPolicyService(db, tx);
    }

    @Bean
    TriggerService triggerService(
            JdbcTriggerRepository db,
            SubmitTaskService submission,
            AgentService agents,
            TransactionTemplate tx,
            @Value("${financeclaw.scheduler.misfire-grace-seconds:60}") long grace) {
        return new TriggerService(db, new SpringTriggerCadence(), submission, agents, tx, grace);
    }

    @Bean
    @ConditionalOnProperty(name = "financeclaw.scheduler.dispatch-enabled", havingValue = "true")
    CommandDispatch commandDispatch(JdbcCommandDispatch db) {
        // Separate bean type avoids enabling fair dispatch merely by registering the management
        // adapter.
        return new CommandDispatch() {
            public com.financeclaw.domain.Task.Command next(String worker) {
                return db.next(worker);
            }

            public void claimed(
                    com.financeclaw.domain.Task.Command c,
                    com.financeclaw.domain.Task.Turn t,
                    String worker) {
                db.claimed(c, t, worker);
            }
        };
    }

    @Bean
    @ConditionalOnProperty(name = "financeclaw.scheduler.enabled", havingValue = "true")
    TriggerPoller triggerPoller(TriggerService service) {
        return new TriggerPoller(service);
    }

    static final class TriggerPoller {
        private final TriggerService service;
        private final AtomicBoolean active = new AtomicBoolean();

        TriggerPoller(TriggerService service) {
            this.service = service;
        }

        @Scheduled(fixedDelayString = "${financeclaw.scheduler.poll-ms:1000}")
        public void poll() {
            if (!active.compareAndSet(false, true)) return;
            try {
                for (int i = 0; i < 20; i++) if (!service.tick()) break;
            } finally {
                active.set(false);
            }
        }
    }
}
