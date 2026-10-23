package com.financeclaw.m1;

import com.financeclaw.app.artifact.ArtifactService;
import com.financeclaw.app.execution.*;
import com.financeclaw.app.execution.port.*;
import com.financeclaw.app.governance.GrantService;
import com.financeclaw.app.interaction.AnswerInteractionService;
import com.financeclaw.app.release.ReleaseService;
import com.financeclaw.app.report.ReportService;
import com.financeclaw.app.task.*;
import com.financeclaw.fixture.GovernanceFixture;
import com.financeclaw.fixture.TaskFixture;
import com.financeclaw.infrastructure.agentscope.AgentScopeInteractionCheckpoint;
import com.financeclaw.infrastructure.artifact.MyBatisArtifactGateway;
import com.financeclaw.infrastructure.execution.CatalogExecutionRelease;
import com.financeclaw.infrastructure.governance.*;
import com.financeclaw.infrastructure.interaction.*;
import com.financeclaw.infrastructure.release.EnvironmentCredentials;
import com.financeclaw.infrastructure.release.EnvironmentReleaseSource;
import com.financeclaw.infrastructure.release.MyBatisReleaseRepository;
import com.financeclaw.infrastructure.release.ReleaseMapper;
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
    public final WorkSignals signals = new WorkSignals();
    public final TaskFixture service;
    public final GovernanceFixture governance;
    public final ReleaseService releases;
    public final ExecutionLeaseService leases;
    public final ExecutionCompletionService completion;
    public final RecoverTaskService recovery;
    public final com.financeclaw.infrastructure.agentscope.capability.BusinessCapabilities business;

    public LifecycleDatabase(DataSource source) {
        try {
            Configuration config = new Configuration();
            config.setMapUnderscoreToCamelCase(true);
            config.setCacheEnabled(false);
            config.setLocalCacheScope(LocalCacheScope.STATEMENT);
            config.addMapper(RuntimeMapper.class);
            config.addMapper(TaskMapper.class);
            config.addMapper(ReleaseMapper.class);
            config.addMapper(com.financeclaw.infrastructure.agent.AgentMapper.class);
            config.addMapper(com.financeclaw.infrastructure.governance.GovernanceMapper.class);
            config.addMapper(LifecycleProbeMapper.class);
            SqlSessionFactoryBean factory = new SqlSessionFactoryBean();
            factory.setDataSource(source);
            factory.setConfiguration(config);
            session = new SqlSessionTemplate(factory.getObject());
            var tx = new TransactionTemplate(new DataSourceTransactionManager(source));
            releases =
                    new ReleaseService(
                            new EnvironmentReleaseSource(
                                    new org.springframework.mock.env.MockEnvironment()),
                            new MyBatisReleaseRepository(session.getMapper(ReleaseMapper.class)),
                            tx);
            var agents =
                    new com.financeclaw.app.agent.AgentService(
                            new com.financeclaw.infrastructure.agent.MyBatisAgentRepository(
                                    session.getMapper(
                                            com.financeclaw.infrastructure.agent.AgentMapper
                                                    .class)),
                            releases,
                            tx);
            var tasks = session.getMapper(TaskMapper.class);
            var gov =
                    session.getMapper(
                            com.financeclaw.infrastructure.governance.GovernanceMapper.class);
            var events = new com.financeclaw.app.task.TaskEventHub();
            var reads = new MyBatisTaskQueryGateway(tasks);
            var mutations = new MyBatisTaskMutationGateway(tasks, events);
            var commands = new MyBatisCommandGateway(tasks);
            var policy = new MyBatisExecutionPolicyGateway(gov);
            var receipts =
                    new MyBatisExecutionReceiptGateway(
                            tasks, session.getMapper(RuntimeMapper.class), gov);
            // M1-M3 fork probes intentionally run without the optional memory module.
            var memory =
                    new ExecutionMemory() {
                        public void admit(com.financeclaw.domain.Task.Turn turn) {}

                        public void check(com.financeclaw.domain.Task.Turn turn) {}

                        public boolean sameEpoch(com.financeclaw.domain.Task.Turn turn) {
                            return true;
                        }

                        @Override
                        public java.util.List<com.financeclaw.domain.Task.Turn> filterHistory(
                                com.financeclaw.domain.Task.Turn current,
                                java.util.List<com.financeclaw.domain.Task.Turn> history) {
                            return java.util.List.copyOf(history);
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
                            new MyBatisTaskAdmissionGateway(tasks, events, signals),
                            policy,
                            release,
                            memory,
                            tx,
                            agents);
            var cancellation =
                    new CancelTaskService(queries, reads, mutations, commands, transitions, tx);
            leases = new ExecutionLeaseService(commands, mutations, reads, transitions, tx);
            var policies =
                    new ExecutionPolicyService(reads, events, policy, release, memory, leases);
            completion =
                    new ExecutionCompletionService(
                            mutations, commands, receipts, leases, policies, transitions);
            recovery =
                    new RecoverTaskService(
                            commands, mutations, receipts, completion, transitions, tx);
            var steps =
                    new CapabilityStepService(
                            new MyBatisCapabilityStepGateway(gov), leases, policies);
            var credentials =
                    new EnvironmentCredentials(new org.springframework.mock.env.MockEnvironment());
            service =
                    new TaskFixture(
                            submission,
                            queries,
                            cancellation,
                            leases,
                            policies,
                            completion,
                            recovery,
                            new MyBatisExecutionContextGateway(gov),
                            releases,
                            credentials);
            var ziweiConfiguration =
                    new com.financeclaw.infrastructure.ziwei.CatalogZiweiConfiguration(
                            releases, credentials);
            var ziweiInputs =
                    new com.financeclaw.app.ziwei.ZiweiInputService(
                            ziweiConfiguration,
                            new MyBatisExecutionContextGateway(gov),
                            new com.financeclaw.infrastructure.agentscope.capability
                                    .AgentScopeZiweiInputs());
            var answers =
                    new AnswerInteractionService(
                            new MyBatisInteractionGateway(
                                    queries,
                                    policies,
                                    transitions,
                                    session.getMapper(TaskMapper.class),
                                    session.getMapper(
                                            com.financeclaw.infrastructure.governance
                                                    .GovernanceMapper.class),
                                    signals),
                            new AgentScopeInteractionCheckpoint(
                                    session.getMapper(RuntimeMapper.class)),
                            new AgentScopeAnswerRules(
                                    java.util.List.of(new ZiweiAnswerValidator(ziweiInputs))),
                            tx);
            var grants = new GrantService(queries, new MyBatisGrantGateway(gov), tx, events);
            var artifacts =
                    new ArtifactService(
                            new MyBatisArtifactGateway(gov, tasks), leases, policies, releases, tx);
            var reports = new ReportService(new MyBatisReportGateway(gov), leases, policies);
            governance = new GovernanceFixture(grants, artifacts, answers, reports);
            var market =
                    new com.financeclaw.app.market.MarketDataService(
                            policies,
                            new com.financeclaw.infrastructure.market.CatalogMarketData(releases));
            var portfolio =
                    new com.financeclaw.app.portfolio.PortfolioReviewService(
                            steps,
                            policies,
                            market,
                            new com.financeclaw.infrastructure.portfolio
                                    .GovernedPortfolioPublication(artifacts));
            business =
                    new com.financeclaw.infrastructure.agentscope.capability.BusinessCapabilities(
                            market,
                            portfolio,
                            new com.financeclaw.app.research.MarketResearchService(
                                    policies,
                                    new ChildInvocationService(
                                            new MyBatisChildInvocationGateway(gov),
                                            steps,
                                            policies),
                                    market),
                            policies,
                            session.getMapper(RuntimeMapper.class),
                            new com.financeclaw.app.ziwei.ZiweiService(
                                    policies,
                                    steps,
                                    ziweiInputs,
                                    ziweiConfiguration,
                                    new com.financeclaw.infrastructure.ziwei
                                            .GovernedZiweiPublication(artifacts)));
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }
}
