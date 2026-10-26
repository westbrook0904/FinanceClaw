package com.financeclaw.bootstrap;

import com.financeclaw.api.ResourceAccessController;
import com.financeclaw.app.agent.AgentService;
import com.financeclaw.app.agent.port.AgentAccessGateway;
import com.financeclaw.app.agent.port.CredentialAccessGateway;
import com.financeclaw.app.artifact.ArtifactService;
import com.financeclaw.app.execution.ChildInvocationService;
import com.financeclaw.app.interaction.AnswerInteractionService;
import com.financeclaw.app.market.MarketDataService;
import com.financeclaw.app.release.ReleaseService;
import com.financeclaw.app.task.TaskQueryService;
import com.financeclaw.infrastructure.agent.ResourceAccessService;
import com.financeclaw.infrastructure.agent.ServerCredentialCatalog;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.beans.factory.config.BeanPostProcessor;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.context.annotation.Import;
import org.springframework.transaction.support.TransactionTemplate;

/**
 * Explicit production wiring keeps application ports framework-independent and fixture construction
 * explicit.
 */
@Configuration(proxyBeanMethods = false)
@Import({
    ResourceAccessService.class,
    ServerCredentialCatalog.class,
    ResourceAccessController.class
})
public class ResourceAuthorizationConfiguration {
    @Bean
    static BeanPostProcessor executionResourceAuthorization(
            ObjectProvider<AgentAccessGateway> agents,
            ObjectProvider<CredentialAccessGateway> credentials,
            ObjectProvider<ReleaseService> releases,
            ObjectProvider<TransactionTemplate> transactions) {
        return new BeanPostProcessor() {
            @Override
            public Object postProcessAfterInitialization(Object bean, String name) {
                if (bean instanceof AgentService service)
                    service.configureAgentAccess(agents.getObject());
                if (bean instanceof ChildInvocationService service) {
                    service.configureAgentAccess(agents.getObject());
                    service.configureCredentialAccess(credentials.getObject());
                }
                if (bean instanceof ArtifactService service)
                    service.configureAgentAccess(agents.getObject());
                if (bean instanceof MarketDataService service)
                    service.configureAgentAccess(agents.getObject());
                if (bean instanceof TaskQueryService service)
                    service.configureResultAccess(
                            agents.getObject(), releases.getObject(), transactions.getObject());
                if (bean instanceof AnswerInteractionService service)
                    service.configureResultAccess(
                            agents.getObject(), releases.getObject(), transactions.getObject());
                return bean;
            }
        };
    }
}
