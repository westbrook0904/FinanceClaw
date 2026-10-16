package com.financeclaw.m1;

import com.financeclaw.app.execution.*;
import com.financeclaw.app.execution.port.*;
import com.financeclaw.app.interaction.AnswerInteractionService;
import com.financeclaw.app.task.*;
import com.financeclaw.application.ReleaseCatalog;
import com.financeclaw.application.TaskService;
import com.financeclaw.infrastructure.agentscope.AgentScopeInteractionCheckpoint;
import com.financeclaw.infrastructure.execution.CatalogExecutionRelease;
import com.financeclaw.infrastructure.interaction.*;
import com.financeclaw.infrastructure.state.RuntimeMapper;
import com.financeclaw.infrastructure.task.*;
import javax.sql.DataSource;
import org.apache.ibatis.session.Configuration;
import org.apache.ibatis.session.LocalCacheScope;
import org.mybatis.spring.*;
import org.springframework.jdbc.datasource.DataSourceTransactionManager;
import org.springframework.transaction.support.TransactionTemplate;

public final class LifecycleDatabase {
  public final SqlSessionTemplate session;
  public final TaskService service;
  public final com.financeclaw.application.GovernanceService governance;
  public final ReleaseCatalog releases = new ReleaseCatalog();
  public final ExecutionLeaseService leases;
  public final ExecutionCompletionService completion;
  public final RecoverTaskService recovery;
  public final ProjectTaskService projection;
  public final com.financeclaw.infrastructure.agentscope.capability.BusinessCapabilities business;

  public LifecycleDatabase(DataSource source) {
    try {
      Configuration config = new Configuration();
      config.setMapUnderscoreToCamelCase(true);
      config.setCacheEnabled(false);
      config.setLocalCacheScope(LocalCacheScope.STATEMENT);
      config.addMapper(RuntimeMapper.class);
      config.addMapper(TaskMapper.class);
      config.addMapper(com.financeclaw.infrastructure.governance.GovernanceMapper.class);
      config.addMapper(LifecycleProbeMapper.class);
      SqlSessionFactoryBean factory = new SqlSessionFactoryBean();
      factory.setDataSource(source);
      factory.setConfiguration(config);
      session = new SqlSessionTemplate(factory.getObject());
      var tx = new TransactionTemplate(new DataSourceTransactionManager(source));
      var tasks = session.getMapper(TaskMapper.class);
      var gov = session.getMapper(com.financeclaw.infrastructure.governance.GovernanceMapper.class);
      var reads = new MyBatisTaskQueryGateway(tasks);
      var mutations = new MyBatisTaskMutationGateway(tasks);
      var commands = new MyBatisCommandGateway(tasks);
      var policy = new MyBatisExecutionPolicyGateway(gov);
      var receipts =
          new MyBatisExecutionReceiptGateway(tasks, session.getMapper(RuntimeMapper.class), gov);
      // M1-M3 fork probes intentionally run without the optional memory module.
      var memory =
          new ExecutionMemory() {
            public void admit(com.financeclaw.domain.Task.Turn turn) {}

            public void check(com.financeclaw.domain.Task.Turn turn) {}

            public boolean sameEpoch(com.financeclaw.domain.Task.Turn turn) {
              return true;
            }

            public void completed(String turn) {}

            public String context(com.financeclaw.domain.Task.Turn turn) {
              return "";
            }
          };
      var release = new CatalogExecutionRelease(releases);
      var transitions = new TaskTransitionService(mutations, policy, memory);
      var queries = new TaskQueryService(reads);
      var submission =
          new SubmitTaskService(
              new MyBatisTaskAdmissionGateway(tasks), policy, release, memory, tx);
      var cancellation =
          new CancelTaskService(queries, reads, mutations, commands, policy, transitions, tx);
      leases = new ExecutionLeaseService(commands, mutations, reads, transitions, tx);
      var policies = new ExecutionPolicyService(reads, mutations, policy, release, memory, leases);
      completion =
          new ExecutionCompletionService(
              mutations, commands, receipts, leases, policies, transitions);
      recovery = new RecoverTaskService(commands, mutations, receipts, completion, transitions, tx);
      projection = new ProjectTaskService(new MyBatisTaskProjectionGateway(tasks), tx);
      var steps =
          new CapabilityStepService(new MyBatisCapabilityStepGateway(gov), leases, policies);
      service =
          new TaskService(
              submission,
              queries,
              cancellation,
              leases,
              policies,
              completion,
              recovery,
              projection,
              steps,
              new MyBatisExecutionContextGateway(gov),
              releases);
      var answers =
          new AnswerInteractionService(
              new MyBatisInteractionGateway(
                  queries,
                  policies,
                  transitions,
                  session.getMapper(TaskMapper.class),
                  session.getMapper(
                      com.financeclaw.infrastructure.governance.GovernanceMapper.class)),
              new AgentScopeInteractionCheckpoint(session.getMapper(RuntimeMapper.class)),
              new AgentScopeAnswerRules(
                  java.util.List.of(
                      new ZiweiAnswerValidator(
                          service,
                          session.getMapper(
                              com.financeclaw.infrastructure.governance.GovernanceMapper.class)))),
              tx);
      governance =
          new com.financeclaw.application.GovernanceService(
              service,
              session.getMapper(TaskMapper.class),
              session.getMapper(com.financeclaw.infrastructure.governance.GovernanceMapper.class),
              answers,
              tx);
      var market =
          new com.financeclaw.app.market.MarketDataService(
              policies, new com.financeclaw.infrastructure.market.CatalogMarketData(releases));
      var portfolio =
          new com.financeclaw.app.portfolio.PortfolioReviewService(
              steps,
              policies,
              market,
              new com.financeclaw.infrastructure.portfolio.GovernedPortfolioPublication(
                  governance));
      business =
          new com.financeclaw.infrastructure.agentscope.capability.BusinessCapabilities(
              market,
              portfolio,
              new com.financeclaw.app.research.MarketResearchService(policies, steps, market),
              policies,
              leases,
              session.getMapper(RuntimeMapper.class));
    } catch (Exception e) {
      throw new IllegalStateException(e);
    }
  }
}
