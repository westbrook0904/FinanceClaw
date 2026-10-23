package com.financeclaw.architecture;

import static com.tngtech.archunit.lang.syntax.ArchRuleDefinition.noClasses;
import static org.assertj.core.api.Assertions.assertThat;

import com.tngtech.archunit.core.domain.JavaClasses;
import com.tngtech.archunit.core.importer.ClassFileImporter;
import com.tngtech.archunit.core.importer.ImportOption;
import java.sql.Connection;
import java.sql.Savepoint;
import org.junit.jupiter.api.Test;

/** Executable COLA boundaries, including a zero-cycle production package graph. */
class ArchitectureTest {
    private static final JavaClasses PRODUCTION =
            new ClassFileImporter()
                    .withImportOption(ImportOption.Predefined.DO_NOT_INCLUDE_TESTS)
                    .importPackages(
                            "com.financeclaw",
                            "com.financeclawmemory",
                            "com.financeclawsetup",
                            "com.financeclawprivacy",
                            "com.financeclawrelease",
                            "com.financeclawagent");

    @Test
    void productionPackagesHaveNoCycles() {
        assertThat(PackageCycles.edges(PRODUCTION))
                .as("class dependencies participating in production package cycles")
                .isEmpty();
    }

    @Test
    void domainDoesNotDependOnFrameworksOrOuterLayers() {
        noClasses()
                .that()
                .resideInAPackage("com.financeclaw.domain..")
                .should()
                .dependOnClassesThat()
                .resideOutsideOfPackages("com.financeclaw.domain..", "java..")
                .because(
                        "domain rules must remain independent of HTTP, persistence and the Agent SDK")
                .check(PRODUCTION);
    }

    @Test
    void applicationCodeLeavesJdbcTransactionControlToSpring() {
        noClasses()
                .should()
                .callMethod(Connection.class, "commit")
                .orShould()
                .callMethod(Connection.class, "rollback")
                .orShould()
                .callMethod(Connection.class, "rollback", Savepoint.class)
                .orShould()
                .callMethod(Connection.class, "setAutoCommit", boolean.class)
                .because("short transactions belong to Spring, not application-owned JDBC commits")
                .check(PRODUCTION);
    }

    @Test
    void apiDoesNotDependOnPersistence() {
        noClasses()
                .that()
                .resideInAPackage("com.financeclaw.api..")
                .should()
                .dependOnClassesThat()
                .resideInAPackage("com.financeclaw.infrastructure..")
                .because("HTTP adapters invoke application use cases instead of mappers")
                .check(PRODUCTION);
    }

    @Test
    void applicationPackagesOnlyUseTheirPortsAndDomain() {
        noClasses()
                .that()
                .resideInAPackage("com.financeclaw.app..")
                .should()
                .dependOnClassesThat()
                .resideOutsideOfPackages(
                        "com.financeclaw.app..",
                        "com.financeclaw.domain..",
                        "com.financeclaw.ziwei..",
                        "com.financeclaw.support..",
                        "java..",
                        "org.springframework.stereotype..",
                        "org.springframework.transaction..",
                        // Application event delivery uses Reactor; SQL and AgentScope stay behind
                        // adapters.
                        "reactor.core.publisher..",
                        "reactor.core.scheduler..",
                        "org.reactivestreams..")
                .because(
                        "Spring manages application transactions; SQL and AgentScope remain behind gateways")
                .check(PRODUCTION);
        noClasses()
                .that()
                .resideInAPackage("com.financeclaw.app..port..")
                .should()
                .dependOnClassesThat()
                .resideOutsideOfPackages(
                        "com.financeclaw.app..port..",
                        "com.financeclaw.domain..",
                        "com.financeclaw.ziwei..",
                        "java..")
                .check(PRODUCTION);
    }

    @Test
    void sharedPayloadContractsDoNotPullApplicationOrAgentCodeBackIn() {
        noClasses()
                .that()
                .resideInAPackage("com.financeclaw.support..")
                .should()
                .dependOnClassesThat()
                .resideOutsideOfPackages(
                        "com.financeclaw.support..",
                        "com.financeclaw.domain..",
                        "com.fasterxml.jackson..",
                        "javax.crypto..",
                        "java..")
                .check(PRODUCTION);
    }

    @Test
    void productionDoesNotContainOrDependOnM0Probes() {
        assertThat(PRODUCTION.stream().map(c -> c.getSimpleName()))
                .doesNotContain(
                        "GuardedAgentRunner", "ExecutionGate", "EffectLedger", "M0RuntimeMapper");
        noClasses()
                .should()
                .dependOnClassesThat()
                .resideInAPackage("com.financeclaw.m0..")
                .check(PRODUCTION);
    }

    @Test
    void governanceUseCasesDoNotDependOnPersistenceOrTheAgentSdk() {
        noClasses()
                .that()
                .resideInAnyPackage(
                        "com.financeclaw.app.interaction..",
                        "com.financeclaw.app.governance..",
                        "com.financeclaw.app.artifact..",
                        "com.financeclaw.app.report..")
                .should()
                .dependOnClassesThat()
                .resideInAnyPackage("com.financeclaw.infrastructure..", "io.agentscope..")
                .because(
                        "governance operations use application ports; SQL and native execution stay outside")
                .check(PRODUCTION);
    }

    @Test
    void taskEntrypointsUseApplicationServicesAndDoNotKnowTheNativeExecutor() {
        noClasses()
                .that()
                .resideInAPackage("com.financeclaw.worker..")
                .or()
                .haveFullyQualifiedName("com.financeclaw.api.TaskController")
                .or()
                .haveFullyQualifiedName("com.financeclaw.api.TaskEvents")
                .should()
                .dependOnClassesThat()
                .resideInAnyPackage("com.financeclaw.infrastructure..", "io.agentscope..")
                .check(PRODUCTION);
        assertThat(com.financeclaw.app.execution.port.AgentExecutionPort.class)
                .isAssignableFrom(
                        com.financeclaw.infrastructure.agentscope.execution.TaskAgentRuntime.class);
        assertThat(PRODUCTION.stream().map(c -> c.getPackageName()))
                .noneMatch(
                        name ->
                                name.equals("com.financeclaw.agent")
                                        || name.startsWith("com.financeclaw.agent."));
    }

    @Test
    void productionDoesNotDependOnTestFixtures() {
        noClasses()
                .should()
                .dependOnClassesThat()
                .resideInAPackage("com.financeclaw.fixture..")
                .check(PRODUCTION);
    }

    @Test
    void businessAdaptersDoNotDependOnHttpOrBootstrap() {
        noClasses()
                .that()
                .haveSimpleName("MarketToolContribution")
                .or()
                .haveSimpleName("PortfolioToolContribution")
                .or()
                .haveSimpleName("BusinessCapabilities")
                .or()
                .haveSimpleName("MarketResearchAgent")
                .or()
                .haveSimpleName("PortfolioDriver")
                .or()
                .haveSimpleName("PortfolioInputs")
                .or()
                .haveSimpleName("ZiweiToolContribution")
                .or()
                .haveSimpleName("AgentScopeZiweiInputs")
                .or()
                .haveSimpleName("ZiweiAnswerValidator")
                .should()
                .dependOnClassesThat()
                .resideInAnyPackage("com.financeclaw.api..", "com.financeclaw.bootstrap..")
                .check(PRODUCTION);
    }

    @Test
    void ziweiComputationKernelHasNoResourcePersistenceOrAgentDependencies() {
        noClasses()
                .that()
                .resideInAPackage("com.financeclaw.app..")
                .and()
                .resideOutsideOfPackage("com.financeclaw.app.ziwei..")
                .should()
                .dependOnClassesThat()
                .resideInAPackage("com.financeclaw.ziwei..")
                .check(PRODUCTION);
        // Keep the frozen calculation package stable: it is a bounded domain library using
        // the pinned lunar arithmetic dependency and shared deterministic JSON/hash codec.
        noClasses()
                .that()
                .resideInAPackage("com.financeclaw.ziwei..")
                .should()
                .dependOnClassesThat()
                .resideOutsideOfPackages(
                        "com.financeclaw.ziwei..",
                        "com.financeclaw.domain..",
                        "com.financeclaw.support..",
                        "com.nlf.calendar..",
                        "java..")
                .check(PRODUCTION);
        noClasses()
                .that()
                .resideInAPackage("com.financeclaw.ziwei..")
                .should()
                .dependOnClassesThat()
                .resideInAnyPackage("java.io..", "java.nio.file..", "java.net..")
                .check(PRODUCTION);
    }

    @Test
    void memoryAndReleaseKeepNativeTypesAndResourceAccessOutsideApplicationContracts() {
        noClasses()
                .that()
                .resideInAnyPackage("com.financeclaw.app.memory..", "com.financeclaw.app.release..")
                .should()
                .dependOnClassesThat()
                .resideInAnyPackage("java.io..", "java.nio.file..", "java.net..", "io.agentscope..")
                .check(PRODUCTION);
        assertThat(com.financeclaw.app.execution.port.ExecutionMemory.class)
                .isAssignableFrom(com.financeclaw.app.memory.MemoryService.class);
    }

    @Test
    void memoryModelsCannotReachBusinessStateOrTools() {
        noClasses()
                .that()
                .resideInAPackage("com.financeclaw.infrastructure.agentscope.memory..")
                .should()
                .dependOnClassesThat()
                .resideInAnyPackage(
                        "com.financeclaw.infrastructure.memory..",
                                "com.financeclaw.infrastructure.task..",
                        "com.financeclaw.infrastructure.governance..",
                                "com.financeclaw.infrastructure.state..",
                        "com.financeclaw.infrastructure.channel..",
                                "com.financeclaw.infrastructure.agentscope.capability..")
                .because(
                        "memory model adapters receive only extraction text and an embedding profile port")
                .check(PRODUCTION);
    }

    @Test
    void applicationAssemblyUsesExplicitImportsInsteadOfScanning() {
        noClasses()
                .should()
                .beMetaAnnotatedWith(org.springframework.context.annotation.ComponentScan.class)
                .orShould()
                .beAnnotatedWith(org.springframework.context.annotation.ComponentScan.class)
                .orShould()
                .beAnnotatedWith(org.mybatis.spring.annotation.MapperScan.class)
                .check(PRODUCTION);
    }

    @Test
    void notificationDeliveryDoesNotConstructAnAgentExecutor() {
        noClasses()
                .that()
                .haveFullyQualifiedName("com.financeclaw.channel.FeishuDelivery")
                .should()
                .dependOnClassesThat()
                .resideInAnyPackage("com.financeclaw.infrastructure.agentscope.execution..")
                .check(PRODUCTION);
        noClasses()
                .that()
                .haveFullyQualifiedName("com.financeclaw.api.FeishuController")
                .should()
                .dependOnClassesThat()
                .haveFullyQualifiedName("com.financeclaw.channel.FeishuDelivery")
                .orShould()
                .dependOnClassesThat()
                .haveFullyQualifiedName("com.financeclaw.channel.AgentScopeFeishu")
                .check(PRODUCTION);
    }
}
