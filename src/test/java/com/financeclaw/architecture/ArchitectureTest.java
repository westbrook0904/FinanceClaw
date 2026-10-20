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
          .importPackages("com.financeclaw", "com.financeclawmemory", "com.financeclawmigrate");

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
        .because("domain rules must remain independent of HTTP, persistence and the Agent SDK")
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
  void migratedApplicationPackagesOnlyUseTheirPortsAndDomain() {
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
            "org.springframework.transaction..")
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
        .doesNotContain("GuardedAgentRunner", "ExecutionGate", "EffectLedger", "M0RuntimeMapper");
    noClasses()
        .should()
        .dependOnClassesThat()
        .resideInAPackage("com.financeclaw.m0..")
        .check(PRODUCTION);
  }

  @Test
  void governanceUseCasesDoNotDependOnLegacyCompositionOrPersistence() {
    noClasses()
        .that()
        .resideInAnyPackage(
            "com.financeclaw.app.interaction..",
            "com.financeclaw.app.governance..",
            "com.financeclaw.app.artifact..",
            "com.financeclaw.app.report..")
        .should()
        .dependOnClassesThat()
        .resideInAnyPackage(
            "com.financeclaw.application..", "com.financeclaw.infrastructure..", "io.agentscope..")
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
        .resideInAnyPackage(
            "com.financeclaw.application..", "com.financeclaw.infrastructure..", "io.agentscope..")
        .check(PRODUCTION);
    assertThat(com.financeclaw.app.execution.port.AgentExecutionPort.class)
        .isAssignableFrom(
            com.financeclaw.infrastructure.agentscope.execution.TaskAgentRuntime.class);
    assertThat(PRODUCTION.stream().map(c -> c.getPackageName()))
        .noneMatch(
            name ->
                name.equals("com.financeclaw.agent") || name.startsWith("com.financeclaw.agent."));
  }

  @Test
  void governanceAndChannelEntrypointsCannotReintroduceTheLegacyTaskFacade() {
    noClasses()
        .that()
        .resideInAnyPackage("com.financeclaw.api..", "com.financeclaw.channel..")
        .should()
        .dependOnClassesThat()
        .haveFullyQualifiedName("com.financeclaw.application.TaskService")
        .check(PRODUCTION);
    noClasses()
        .that()
        .haveFullyQualifiedName("com.financeclaw.channel.FeishuService")
        .should()
        .dependOnClassesThat()
        .resideInAPackage("com.financeclaw.application..")
        .check(PRODUCTION);
  }

  @Test
  void productionCannotDependOnTheRemovedFacadeOrTestFixtures() {
    assertThat(PRODUCTION.stream().map(c -> c.getName()))
        .doesNotContain(
            "com.financeclaw.application.TaskService",
            "com.financeclaw.application.GovernanceService");
    noClasses()
        .should()
        .dependOnClassesThat()
        .resideInAPackage("com.financeclaw.application..")
        .orShould()
        .dependOnClassesThat()
        .resideInAPackage("com.financeclaw.fixture..")
        .check(PRODUCTION);
  }

  @Test
  void migratedBusinessAdaptersCannotReachTheLegacyTaskFacade() {
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
        .resideInAnyPackage("com.financeclaw.application..", "com.financeclaw.tools..")
        .check(PRODUCTION);
    assertThat(PRODUCTION.stream().map(c -> c.getName()))
        .doesNotContain(
            "com.financeclaw.tools.PortfolioReview",
            "com.financeclaw.tools.MarketData",
            "com.financeclaw.tools.MarketResearch",
            "com.financeclaw.tools.ZiweiTools",
            "com.financeclaw.tools.CapabilitySteps");
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
  void memoryAndReleaseHaveNoLegacyOrNativeTypesInApplicationContracts() {
    assertThat(PRODUCTION.stream().map(c -> c.getName()))
        .doesNotContain(
            "com.financeclaw.application.MemoryService",
            "com.financeclaw.application.ReleaseCatalog",
            "com.financeclaw.tools.BusinessCatalog",
            "com.financeclaw.memory.MemoryModels",
            "com.financeclaw.memory.MemoryProcessor",
            "com.financeclaw.memory.MemorySearch",
            "com.financeclaw.infrastructure.execution.GovernedExecutionMemory",
            "com.financeclaw.ziwei.ZiweiPolicy");
    noClasses()
        .that()
        .resideInAnyPackage("com.financeclaw.app.memory..", "com.financeclaw.app.release..")
        .should()
        .dependOnClassesThat()
        .resideInAnyPackage(
            "java.io..", "java.nio.file..", "java.net..", "com.financeclaw.application..")
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
            "com.financeclaw.application..", "com.financeclaw.tools..",
            "com.financeclaw.infrastructure.memory..", "com.financeclaw.infrastructure.task..",
            "com.financeclaw.infrastructure.governance..", "com.financeclaw.infrastructure.state..",
            "com.financeclaw.infrastructure.channel..",
                "com.financeclaw.infrastructure.agentscope.capability..")
        .because("memory model adapters receive only extraction text and an embedding profile port")
        .check(PRODUCTION);
  }

  @Test
  void roleAssemblyCannotFallBackToComponentOrMapperScanning() {
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
  void notificationDeliveryAndProjectionDoNotConstructAnAgentExecutor() {
    noClasses()
        .that()
        .haveFullyQualifiedName("com.financeclaw.channel.FeishuDelivery")
        .or()
        .haveFullyQualifiedName("com.financeclaw.worker.OutboxProjector")
        .should()
        .dependOnClassesThat()
        .resideInAnyPackage(
            "com.financeclaw.application..",
            "com.financeclaw.infrastructure.agentscope.execution..")
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
