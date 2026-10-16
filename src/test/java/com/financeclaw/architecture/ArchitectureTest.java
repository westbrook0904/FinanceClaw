package com.financeclaw.architecture;

import static com.tngtech.archunit.lang.syntax.ArchRuleDefinition.noClasses;
import static org.assertj.core.api.Assertions.assertThat;

import com.tngtech.archunit.core.domain.JavaClasses;
import com.tngtech.archunit.core.importer.ClassFileImporter;
import com.tngtech.archunit.core.importer.ImportOption;
import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.sql.Savepoint;
import java.util.Set;
import java.util.TreeSet;
import org.junit.jupiter.api.Test;

/**
 * Executable boundaries for the incremental COLA migration; existing cycles remain explicit debt.
 */
class ArchitectureTest {
  private static final JavaClasses PRODUCTION =
      new ClassFileImporter()
          .withImportOption(ImportOption.Predefined.DO_NOT_INCLUDE_TESTS)
          .importPackages("com.financeclaw", "com.financeclawmemory", "com.financeclawmigrate");

  @Test
  void packageCyclesMayOnlyLosePreviouslyRecordedClassDependencies() throws Exception {
    try (var in = getClass().getResourceAsStream("/architecture/legacy-package-cycles.txt")) {
      assertThat(in).isNotNull();
      Set<String> baseline =
          new TreeSet<>(new String(in.readAllBytes(), StandardCharsets.UTF_8).lines().toList());
      var added = new TreeSet<>(PackageCycles.edges(PRODUCTION));
      added.removeAll(baseline);
      assertThat(added).as("new class dependencies participating in package cycles").isEmpty();
    }
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
            "com.financeclaw.app..port..", "com.financeclaw.domain..", "java..")
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
  void interactionUseCaseAndBusinessToolsDoNotDependOnLegacyAgentComposition() {
    noClasses()
        .that()
        .resideInAnyPackage("com.financeclaw.application..", "com.financeclaw.tools..")
        .should()
        .dependOnClassesThat()
        .resideInAPackage("com.financeclaw.infrastructure.agentscope.execution..")
        .because(
            "SDK helpers live in infrastructure; tools do not depend on their composition root")
        .check(PRODUCTION);
    noClasses()
        .that()
        .haveFullyQualifiedName("com.financeclaw.application.GovernanceService")
        .should()
        .dependOnClassesThat()
        .resideInAnyPackage("io.agentscope..", "com.financeclaw.ziwei..")
        .because(
            "answer coordination delegates snapshot and business rules through application ports")
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
  void legacyTaskFacadeCannotExposePersistenceOrOwnTransactions() {
    noClasses()
        .that()
        .haveFullyQualifiedName("com.financeclaw.application.TaskService")
        .should()
        .dependOnClassesThat()
        .resideInAnyPackage(
            "com.financeclaw.infrastructure..",
            "org.springframework.transaction..",
            "io.agentscope..")
        .check(PRODUCTION);
    assertThat(com.financeclaw.application.TaskService.class.getDeclaredMethods())
        .noneMatch(method -> Set.of("governance", "releases").contains(method.getName()));
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
        .should()
        .dependOnClassesThat()
        .resideInAnyPackage("com.financeclaw.application..", "com.financeclaw.tools..")
        .check(PRODUCTION);
    assertThat(PRODUCTION.stream().map(c -> c.getName()))
        .doesNotContain(
            "com.financeclaw.tools.PortfolioReview",
            "com.financeclaw.tools.MarketData",
            "com.financeclaw.tools.MarketResearch");
  }
}
