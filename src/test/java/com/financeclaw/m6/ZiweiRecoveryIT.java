package com.financeclaw.m6;

import static org.assertj.core.api.Assertions.*;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.financeclaw.agent.ResumeInputs;
import com.financeclaw.application.*;
import com.financeclaw.bootstrap.FinanceClawApplication;
import com.financeclaw.domain.Governance.Answer;
import com.financeclaw.domain.Task.*;
import java.nio.file.*;
import java.time.Duration;
import java.util.*;
import java.util.concurrent.TimeUnit;
import java.util.zip.*;
import javax.sql.DataSource;
import org.junit.jupiter.api.*;
import org.junit.jupiter.api.io.TempDir;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.*;
import org.testcontainers.DockerClientFactory;
import org.testcontainers.containers.*;
import org.testcontainers.containers.startupcheck.OneShotStartupCheckStrategy;
import org.testcontainers.utility.MountableFile;

@SpringBootTest(
    classes = FinanceClawApplication.class,
    properties = {
      "financeclaw.worker.enabled=false", "financeclaw.integrations.enabled=false",
      "financeclaw.memory.enabled=true", "financeclaw.business.ziwei.enabled=true",
      "financeclaw.business.ziwei.development=true",
          "financeclaw.business.ziwei.key-version=test-v1",
      "ZIWEI_HMAC_KEY=synthetic-m6c-key-only-01234567890123456789"
    })
class ZiweiRecoveryIT {
  static final Network NETWORK = Network.newNetwork();
  static final PostgreSQLContainer<?> PG =
      new PostgreSQLContainer<>("postgres:16.10-alpine")
          .withDatabaseName("m6d")
          .withUsername("m6d")
          .withPassword("test-only")
          .withNetwork(NETWORK)
          .withNetworkAliases("m6d-postgres");

  static {
    PG.start();
  }

  @DynamicPropertySource
  static void database(DynamicPropertyRegistry r) {
    r.add("spring.datasource.url", PG::getJdbcUrl);
    r.add("spring.datasource.username", PG::getUsername);
    r.add("spring.datasource.password", PG::getPassword);
  }

  @Autowired TaskService tasks;
  @Autowired GovernanceService governance;
  @Autowired MemoryService memory;
  @Autowired DataSource source;
  @TempDir Path directory;
  final Owner owner = new Owner("local", "m6d-synthetic");
  final Path application = Path.of("target/financeclaw-java-0.1.0-SNAPSHOT.jar").toAbsolutePath();
  JdbcTemplate sql;
  Path launcher;

  @BeforeEach
  void reset() throws Exception {
    sql = new JdbcTemplate(source);
    sql.execute("TRUNCATE business.conversations CASCADE");
    sql.execute("TRUNCATE memory.owners,memory.jobs,memory.operations,memory.audit CASCADE");
    launcher = directory.resolve("probe.jar");
    String resource = "com/financeclaw/m6/ZiweiRuntimeProbe.class";
    try (var zip = new ZipOutputStream(Files.newOutputStream(launcher));
        var bytes = getClass().getResourceAsStream("/" + resource)) {
      zip.putNextEntry(new ZipEntry(resource));
      zip.write(Objects.requireNonNull(bytes).readAllBytes());
      zip.closeEntry();
    }
  }

  @AfterAll
  static void closeDatabase() {
    PG.stop();
    NETWORK.close();
  }

  Accepted submit() {
    var turn = tasks.submit(owner, tasks.createConversation(owner), "once", "合成紫微验收");
    sql.update(
        "UPDATE business.turns SET created_at='2025-12-31T16:00:00Z' WHERE id=?", turn.turnId());
    return turn;
  }

  Turn turn(Accepted t) {
    return tasks.get(owner, t.conversationId(), t.turnId());
  }

  int artifacts() {
    return sql.queryForObject("SELECT count(*) FROM business.artifacts", Integer.class);
  }

  String artifact() {
    return sql.queryForObject("SELECT id FROM business.artifacts", String.class);
  }

  void expire() {
    sql.update(
        "UPDATE business.commands SET lease_until=clock_timestamp()-interval '1 second' WHERE status='running'");
  }

  Properties settings(String mode, boolean missingSex) {
    var p = new Properties();
    p.setProperty("spring.datasource.url", PG.getJdbcUrl());
    p.setProperty("spring.datasource.username", PG.getUsername());
    p.setProperty("spring.datasource.password", PG.getPassword());
    p.setProperty("spring.main.web-application-type", "none");
    p.setProperty("spring.config.import", "");
    p.setProperty("financeclaw.worker.enabled", "false");
    p.setProperty("financeclaw.integrations.enabled", "false");
    p.setProperty("financeclaw.memory.enabled", "true");
    p.setProperty("financeclaw.business.ziwei.enabled", "true");
    p.setProperty("financeclaw.business.ziwei.development", "true");
    p.setProperty("financeclaw.business.ziwei.key-version", "test-v1");
    p.setProperty("ZIWEI_HMAC_KEY", ZiweiDomainTest.KEY);
    p.setProperty("probe.mode", mode);
    p.setProperty("probe.barrier", directory.resolve("barrier").toString());
    var input = ZiweiDomainTest.input();
    input.put("year_offset", 0);
    if (missingSex) ((Map<?, ?>) input.get("birth")).remove("sex_for_chart");
    p.setProperty("probe.input", ResumeInputs.canonical(input));
    return p;
  }

  Path save(Properties p) throws Exception {
    Path config = directory.resolve(UUID.randomUUID() + ".properties");
    try (var out = Files.newOutputStream(config)) {
      p.store(out, "synthetic isolated database only");
    }
    return config;
  }

  Process start(Properties p) throws Exception {
    var builder =
        new ProcessBuilder(
                Path.of(System.getProperty("java.home"), "bin/java").toString(),
                "-Dfile.encoding=UTF-8",
                "-Duser.timezone=Pacific/Auckland",
                "-Dloader.main=" + ZiweiRuntimeProbe.class.getName(),
                "-Dloader.path=" + launcher,
                "-cp",
                application.toString(),
                "org.springframework.boot.loader.launch.PropertiesLauncher",
                save(p).toString())
            .directory(directory.toFile())
            .redirectErrorStream(true)
            .redirectOutput(directory.resolve("process.log").toFile());
    builder.environment().clear();
    builder.environment().put("PATH", "");
    return builder.start();
  }

  void finish(Process process, int exit) throws Exception {
    try {
      assertThat(process.waitFor(60, TimeUnit.SECONDS)).as("fork exits").isTrue();
      assertThat(process.exitValue())
          .withFailMessage("Probe: %s", Files.readString(directory.resolve("process.log")))
          .isEqualTo(exit);
    } finally {
      process.destroyForcibly();
    }
  }

  void run(String mode, boolean missingSex, int exit) throws Exception {
    finish(start(settings(mode, missingSex)), exit);
  }

  void answer(Accepted t) {
    var question = governance.interactions(owner, t.conversationId(), t.turnId()).getFirst();
    var answer =
        new Answer(
            "input",
            question.revision(),
            null,
            null,
            Map.of("birth", Map.of("sex_for_chart", "male")));
    governance.answer(owner, question.id(), "answer-once", answer);
    assertThat(governance.answer(owner, question.id(), "answer-once", answer).idempotentReplay())
        .isTrue();
  }

  @Test
  void sealedSuspensionAndAnswerSurviveIndependentProcessRestarts() throws Exception {
    var t = submit();
    run("after-seal", true, 73);
    assertThat(turn(t).status()).isEqualTo("running");
    expire();
    run("recover", true, 0);
    assertThat(turn(t).status()).isEqualTo("waiting");
    assertThat(artifacts()).isZero();
    answer(t);
    run("normal", true, 0);
    assertThat(turn(t).status()).isEqualTo("completed");
    assertThat(artifacts()).isEqualTo(1);
    assertThat(governance.read(owner, artifact(), 0, 1, "/target").data())
        .isEqualTo(Map.of("start", "2026-01-01", "end", "2027-01-01", "timezone", "Asia/Shanghai"));
    assertThat(sql.queryForObject("SELECT count(*) FROM memory.facts", Integer.class)).isZero();
  }

  @ParameterizedTest
  @ValueSource(strings = {"after-seal", "after-artifact"})
  void completionReceiptDistinguishesRollForwardFromUnconfirmedExecution(String mode)
      throws Exception {
    var t = submit();
    run(mode, false, 73);
    assertThat(artifacts()).isEqualTo(1);
    expire();
    run("recover", false, 0);
    run("recover", false, 0);
    assertThat(turn(t).status()).isEqualTo(mode.equals("after-seal") ? "completed" : "blocked");
    if (mode.equals("after-artifact"))
      assertThat(turn(t).reason()).isEqualTo("unconfirmed_execution");
    assertThat(artifacts()).isEqualTo(1);
    assertThat(sql.queryForObject("SELECT count(*) FROM business.attempts", Integer.class))
        .isEqualTo(1);
    assertThat(
            sql.queryForObject(
                "SELECT count(*) FROM business.journal WHERE role='assistant'", Integer.class))
        .isEqualTo(mode.equals("after-seal") ? 1 : 0);
  }

  @ParameterizedTest
  @ValueSource(strings = {"key", "release", "grant", "privacy", "cancel"})
  void answeredSuspensionCannotBypassChangesWhileWorkerIsStopped(String change) throws Exception {
    var t = submit();
    run("normal", true, 0);
    assertThat(turn(t).status()).isEqualTo("waiting");
    answer(t);
    var p = settings("normal", true);
    switch (change) {
      case "key" -> p.setProperty("ZIWEI_HMAC_KEY", ZiweiDomainTest.KEY + "rotated");
      case "release" -> p.setProperty("financeclaw.business.ziwei.key-version", "next-version");
      case "grant" ->
          governance.changeGrant(
              owner,
              t.conversationId(),
              "revoke",
              governance.grant(owner, t.conversationId()).revision(),
              List.of("artifact:read"));
      case "privacy" -> memory.forget(owner, memory.privacy(owner).epoch(), "forget");
      case "cancel" -> tasks.cancel(owner, t.conversationId(), t.turnId(), "cancel");
      default -> throw new AssertionError(change);
    }
    if (!change.equals("cancel")) finish(start(p), 0);
    assertThat(turn(t).status()).isEqualTo(change.equals("cancel") ? "cancelled" : "blocked");
    if (!change.equals("cancel")) {
      assertThat(turn(t).reason())
          .isEqualTo(
              switch (change) {
                case "key" -> "step_input_conflict";
                case "release" -> "release_unavailable";
                case "grant" -> "authorization_changed";
                case "privacy" -> "memory_privacy_changed";
                default -> throw new AssertionError(change);
              });
    }
    assertThat(artifacts()).isZero();
  }

  @ParameterizedTest
  @ValueSource(
      strings = {
        "cancel-before-tool",
        "cancel-after-tool",
        "privacy-before-tool",
        "privacy-after-tool"
      })
  void cancellationAndPrivacyCompeteWithToolDispatchAndReceipt(String scenario) throws Exception {
    var t = submit();
    String mode = scenario.substring(scenario.indexOf('-') + 1);
    Process child = start(settings(mode, false));
    Path marker = directory.resolve("barrier");
    try {
      long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(40);
      while (!Files.exists(marker) && child.isAlive() && System.nanoTime() < deadline)
        Thread.sleep(20);
      assertThat(Files.exists(marker))
          .withFailMessage("Barrier: %s", Files.readString(directory.resolve("process.log")))
          .isTrue();
      assertThat(artifacts()).isEqualTo(mode.equals("after-tool") ? 1 : 0);
      if (scenario.startsWith("cancel"))
        tasks.cancel(owner, t.conversationId(), t.turnId(), "cancel");
      else memory.forget(owner, memory.privacy(owner).epoch(), "forget");
      Files.writeString(Path.of(marker + ".continue"), "continue");
      finish(child, 0);
      assertThat(turn(t).status())
          .isEqualTo(scenario.startsWith("cancel") ? "cancelled" : "blocked");
      assertThat(artifacts()).isEqualTo(mode.equals("after-tool") ? 1 : 0);
      assertThat(
              sql.queryForObject(
                  "SELECT count(*) FROM business.journal WHERE role='assistant'", Integer.class))
          .isZero();
      if (scenario.equals("privacy-after-tool")) {
        String id = artifact();
        assertThatThrownBy(() -> governance.read(owner, id, 0, 1, "/facts"))
            .hasMessage("privacy_epoch_changed");
      }
    } finally {
      child.destroyForcibly();
    }
  }

  @Test
  void deploymentImageRunsAllFiveNativeToolsWithoutPythonOrFixtureClasspath() throws Exception {
    var t = submit();
    var p = settings("normal", false);
    p.setProperty("spring.datasource.url", "jdbc:postgresql://m6d-postgres:5432/m6d");
    p.setProperty(
        "probe.tools",
        "ziwei_natal_chart,ziwei_decadal_chart,ziwei_yearly_chart,ziwei_monthly_chart,ziwei_daily_chart");
    var input = ZiweiDomainTest.input();
    input.put("on_date", "2026-02-17");
    p.setProperty("probe.input", ResumeInputs.canonical(input));
    String image = "financeclaw-java:m6d-test-" + UUID.randomUUID();
    Process build =
        new ProcessBuilder("docker", "build", "--tag", image, ".")
            .redirectErrorStream(true)
            .redirectOutput(directory.resolve("build.log").toFile())
            .start();
    try {
      assertThat(build.waitFor(180, TimeUnit.SECONDS)).as("deployment image build").isTrue();
      assertThat(build.exitValue())
          .withFailMessage("Build: %s", Files.readString(directory.resolve("build.log")))
          .isZero();
    } finally {
      build.destroyForcibly();
    }
    try (var container =
        new GenericContainer<>(image)
            .withNetwork(NETWORK)
            .withCopyFileToContainer(MountableFile.forHostPath(launcher), "/tmp/probe.jar")
            .withCopyFileToContainer(
                MountableFile.forHostPath(save(p), 0444), "/tmp/probe.properties")
            .withCreateContainerCmdModifier(cmd -> cmd.withEntrypoint("/bin/sh"))
            .withCommand(
                "-c",
                "! command -v python && ! command -v python3 && exec java -Dloader.main=com.financeclaw.m6.ZiweiRuntimeProbe -Dloader.path=/tmp/probe.jar -cp /app/financeclaw.jar org.springframework.boot.loader.launch.PropertiesLauncher /tmp/probe.properties")
            .withStartupCheckStrategy(
                new OneShotStartupCheckStrategy().withTimeout(Duration.ofSeconds(60)))) {
      container.start();
      assertThat(container.getLogs()).contains("M6D_PROBE_DONE");
      assertThat(turn(t).status()).isEqualTo("completed");
      assertThat(artifacts()).isEqualTo(5);
      Path evidence = Path.of("target/m6/runtime-packaging.json");
      Files.createDirectories(evidence.getParent());
      new ObjectMapper()
          .writerWithDefaultPrettyPrinter()
          .writeValue(
              evidence.toFile(),
              Map.of(
                  "image_id",
                  container.getContainerInfo().getImageId(),
                  "native_tools_completed",
                  5,
                  "python_available",
                  false,
                  "model",
                  "scripted-test-only",
                  "production_dockerfile",
                  true,
                  "application_jar_sha256",
                  HexFormat.of()
                      .formatHex(
                          java.security.MessageDigest.getInstance("SHA-256")
                              .digest(Files.readAllBytes(application))),
                  "scope",
                  "Real deployment image, production runtime and PostgreSQL; synthetic model in external test launcher only"));
    } finally {
      DockerClientFactory.instance().client().removeImageCmd(image).withForce(true).exec();
    }
  }
}
