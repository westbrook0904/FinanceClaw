package com.financeclaw.bootstrap;

import com.financeclaw.api.*;
import com.financeclaw.app.artifact.ArtifactService;
import com.financeclaw.app.execution.*;
import com.financeclaw.app.governance.GrantService;
import com.financeclaw.app.interaction.AnswerInteractionService;
import com.financeclaw.app.market.MarketDataService;
import com.financeclaw.app.memory.*;
import com.financeclaw.app.operations.*;
import com.financeclaw.app.portfolio.PortfolioReviewService;
import com.financeclaw.app.release.ReleaseService;
import com.financeclaw.app.release.port.ReleaseRepository;
import com.financeclaw.app.release.port.ReleaseSource;
import com.financeclaw.app.report.ReportService;
import com.financeclaw.app.research.MarketResearchService;
import com.financeclaw.app.task.*;
import com.financeclaw.app.ziwei.*;
import com.financeclaw.channel.*;
import com.financeclaw.infrastructure.agentscope.AgentScopeInteractionCheckpoint;
import com.financeclaw.infrastructure.agentscope.capability.*;
import com.financeclaw.infrastructure.agentscope.execution.TaskAgentRuntime;
import com.financeclaw.infrastructure.agentscope.memory.AgentScopeMemoryModels;
import com.financeclaw.infrastructure.artifact.MyBatisArtifactGateway;
import com.financeclaw.infrastructure.execution.CatalogExecutionRelease;
import com.financeclaw.infrastructure.governance.MyBatisGrantGateway;
import com.financeclaw.infrastructure.governance.MyBatisReportGateway;
import com.financeclaw.infrastructure.interaction.*;
import com.financeclaw.infrastructure.market.CatalogMarketData;
import com.financeclaw.infrastructure.memory.*;
import com.financeclaw.infrastructure.operations.*;
import com.financeclaw.infrastructure.portfolio.GovernedPortfolioPublication;
import com.financeclaw.infrastructure.release.*;
import com.financeclaw.infrastructure.task.*;
import com.financeclaw.infrastructure.ziwei.*;
import com.financeclaw.worker.TaskWorker;
import org.springframework.context.annotation.*;
import org.springframework.transaction.support.TransactionTemplate;

/**
 * 显式装配统一业务上下文及其职责分组，避免全包扫描把离线或受限入口混入业务进程。
 *
 * <p>HTTP、执行和投递共享事件中心与连接池；分组只表达依赖关系，不再提供旧的分体部署 profile。
 */
public final class RoleConfiguration {
    // Keep the holder unannotated: Spring discovers all nested configurations of a component.
    private RoleConfiguration() {}

    /** 统一业务上下文，事件发布与 SSE 订阅使用同一个进程内事件中心。 */
    @Configuration(proxyBeanMethods = false)
    @Import({Http.class, Execution.class, Delivery.class})
    static class Standalone {}

    @Configuration(proxyBeanMethods = false)
    @Import({
        MapperConfiguration.Tasks.class,
        RuntimeHealth.class,
        TaskEventHub.class,
        WorkSignals.class,
        SchedulingConfiguration.class,
        TaskQueryService.class,
        MyBatisTaskQueryGateway.class
    })
    static class TaskReads {}

    @Configuration(proxyBeanMethods = false)
    @Import({
        TaskReads.class,
        MapperConfiguration.Governance.class,
        MapperConfiguration.Runtime.class,
        MapperConfiguration.Memory.class,
        MemoryConfiguration.class,
        MyBatisMemoryRepository.class,
        MapperConfiguration.Releases.class,
        MyBatisReleaseRepository.class,
        EnvironmentReleaseSource.class,
        EnvironmentCredentials.class,
        CatalogExecutionRelease.class,
        MyBatisTaskAdmissionGateway.class,
        MyBatisTaskMutationGateway.class,
        MyBatisCommandGateway.class,
        MyBatisExecutionPolicyGateway.class,
        MyBatisExecutionContextGateway.class,
        SubmitTaskService.class,
        CancelTaskService.class,
        TaskTransitionService.class,
        ExecutionLeaseService.class,
        ExecutionPolicyService.class,
        ArtifactService.class,
        MyBatisArtifactGateway.class,
        AnswerInteractionService.class,
        MyBatisInteractionGateway.class,
        AgentScopeInteractionCheckpoint.class,
        AgentScopeAnswerRules.class,
        ZiweiAnswerValidator.class,
        ZiweiInputService.class,
        CatalogZiweiConfiguration.class,
        AgentScopeZiweiInputs.class
    })
    static class GovernedTasks {
        @Bean
        ReleaseService releaseService(
                ReleaseSource source, ReleaseRepository repository, TransactionTemplate tx) {
            return new ReleaseService(source, repository, tx);
        }
    }

    @Configuration(proxyBeanMethods = false)
    @Import({
        GovernedTasks.class,
        GrantService.class,
        MyBatisGrantGateway.class,
        MapperConfiguration.Operations.class,
        MapperConfiguration.Channel.class,
        AdmissionService.class,
        OperationsQueryService.class,
        MyBatisAdmissionGateway.class,
        MyBatisOperationsReadGateway.class,
        MemorySearchService.class,
        AgentScopeMemoryModels.class,
        MyBatisEmbeddingProfiles.class,
        FeishuSettings.class,
        FeishuIngress.class,
        FeishuService.class,
        BearerIdentity.class,
        HttpRequestConfiguration.class,
        ApiErrors.class,
        TaskController.class,
        TaskEvents.class,
        GovernanceController.class,
        ArtifactContentController.class,
        OperationsController.class,
        MemoryController.class,
        UserProfileController.class,
        FeishuController.class
    })
    static class Http {}

    @Configuration(proxyBeanMethods = false)
    @Import({
        GovernedTasks.class,
        ReportService.class,
        MyBatisReportGateway.class,
        MyBatisExecutionReceiptGateway.class,
        ExecutionCompletionService.class,
        RecoverTaskService.class,
        ExecuteTaskService.class,
        CapabilityStepService.class,
        MyBatisCapabilityStepGateway.class,
        MarketDataService.class,
        CatalogMarketData.class,
        PortfolioReviewService.class,
        GovernedPortfolioPublication.class,
        MarketResearchService.class,
        ZiweiService.class,
        GovernedZiweiPublication.class,
        BusinessCapabilities.class,
        ExecutionSchedulingConfiguration.class,
        TaskAgentRuntime.class,
        TaskWorker.class
    })
    static class Execution {}

    @Configuration(proxyBeanMethods = false)
    @Import({
        TaskReads.class,
        MapperConfiguration.Governance.class,
        MapperConfiguration.Channel.class,
        MyBatisExecutionContextGateway.class,
        FeishuSettings.class,
        AgentScopeFeishu.class,
        FeishuDelivery.class
    })
    static class Delivery {}
}
