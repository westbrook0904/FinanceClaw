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
import com.financeclaw.worker.OutboxProjector;
import com.financeclaw.worker.TaskWorker;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.context.annotation.*;

/** Role composition uses Spring imports/conditions, with no application-wide component scan. */
public final class RoleConfiguration {
  // Keep the holder unannotated: Spring discovers all nested configurations of a component.
  private RoleConfiguration() {}

  @Configuration(proxyBeanMethods = false)
  @ConditionalOnProperty(name = "financeclaw.deployment.role", havingValue = "api")
  @Import(Http.class)
  static class Api {}

  @Configuration(proxyBeanMethods = false)
  @ConditionalOnProperty(name = "financeclaw.deployment.role", havingValue = "worker")
  @Import(Execution.class)
  static class Worker {}

  @Configuration(proxyBeanMethods = false)
  @ConditionalOnProperty(name = "financeclaw.deployment.role", havingValue = "integrations")
  @Import(Delivery.class)
  static class Integrations {}

  @Configuration(proxyBeanMethods = false)
  @ConditionalOnProperty(
      name = "financeclaw.deployment.role",
      havingValue = "standalone",
      matchIfMissing = true)
  @Import({Http.class, Execution.class, Delivery.class})
  static class Standalone {}

  @Configuration(proxyBeanMethods = false)
  @Import({
    MapperConfiguration.Tasks.class,
    RuntimeHealth.class,
    TaskEventHub.class,
    TaskQueryService.class,
    MyBatisTaskQueryGateway.class
  })
  static class TaskReads {}

  @Configuration(proxyBeanMethods = false)
  @Import({ProjectTaskService.class, MyBatisTaskProjectionGateway.class})
  static class Projection {}

  @Configuration(proxyBeanMethods = false)
  @Import({
    TaskReads.class,
    MapperConfiguration.Governance.class,
    MapperConfiguration.Runtime.class,
    MapperConfiguration.Memory.class,
    MemoryConfiguration.class,
    MyBatisMemoryRepository.class,
    ReleaseService.class,
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
  static class GovernedTasks {}

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
    OperationsController.class,
    MemoryController.class,
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
    TaskAgentRuntime.class,
    TaskWorker.class
  })
  static class Execution {}

  @Configuration(proxyBeanMethods = false)
  @Import({
    TaskReads.class,
    Projection.class,
    MapperConfiguration.Governance.class,
    MapperConfiguration.Channel.class,
    MyBatisExecutionContextGateway.class,
    FeishuSettings.class,
    AgentScopeFeishu.class,
    FeishuDelivery.class,
    OutboxProjector.class
  })
  static class Delivery {}
}
