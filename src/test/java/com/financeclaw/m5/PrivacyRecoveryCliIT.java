package com.financeclaw.m5;

import static org.assertj.core.api.Assertions.*;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.financeclaw.domain.memory.ForgetFact;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.attribute.PosixFilePermissions;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.TimeUnit;
import org.flywaydb.core.Flyway;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;
import org.springframework.core.io.ClassPathResource;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DriverManagerDataSource;
import org.springframework.jdbc.datasource.init.ResourceDatabasePopulator;
import org.testcontainers.containers.PostgreSQLContainer;

/**
 * Runs the packaged production CLI in independent JVMs, with no test classes on their classpath.
 */
class PrivacyRecoveryCliIT {
  private static final String MAIN_PASSWORD = "cli-main-password-private-sentinel";
  private static final String LEDGER_PASSWORD = "cli-ledger-password-private-sentinel";
  private static final String TENANT = "cli-tenant-private-sentinel";
  private static final String OWNER = "cli-owner-private-sentinel";
  private static final String CONTENT = "cli-memory-content-private-sentinel";
  private static final String ISOLATED = "--financeclaw.privacy-recovery.isolated=true";
  private static final PostgreSQLContainer<?> MAIN =
      new PostgreSQLContainer<>("postgres:16.10-alpine")
          .withDatabaseName("privacy_cli_main")
          .withUsername("cli_main_user")
          .withPassword(MAIN_PASSWORD);
  private static final PostgreSQLContainer<?> LEDGER =
      new PostgreSQLContainer<>("postgres:16.10-alpine")
          .withDatabaseName("privacy_cli_ledger")
          .withUsername("cli_ledger_user")
          .withPassword(LEDGER_PASSWORD);
  private static JdbcTemplate sql;
  private static JdbcTemplate external;
  private static JdbcTemplate unmigrated;
  private static String unmigratedUrl;
  @TempDir Path directory;

  @BeforeAll
  static void prepare() {
    MAIN.start();
    LEDGER.start();
    var mainSource =
        new DriverManagerDataSource(MAIN.getJdbcUrl(), MAIN.getUsername(), MAIN_PASSWORD);
    var ledgerSource =
        new DriverManagerDataSource(LEDGER.getJdbcUrl(), LEDGER.getUsername(), LEDGER_PASSWORD);
    Flyway.configure().dataSource(mainSource).load().migrate();
    new ResourceDatabasePopulator(new ClassPathResource("privacy-ledger/V1__forget_ledger.sql"))
        .execute(ledgerSource);
    sql = new JdbcTemplate(mainSource);
    external = new JdbcTemplate(ledgerSource);
    sql.execute("CREATE DATABASE privacy_cli_unmigrated");
    unmigratedUrl = MAIN.getJdbcUrl().replace("/privacy_cli_main", "/privacy_cli_unmigrated");
    unmigrated =
        new JdbcTemplate(
            new DriverManagerDataSource(unmigratedUrl, MAIN.getUsername(), MAIN_PASSWORD));
  }

  @AfterAll
  static void stop() {
    LEDGER.stop();
    MAIN.stop();
  }

  @BeforeEach
  void reset() {
    sql.execute("TRUNCATE business.conversations CASCADE");
    sql.execute("TRUNCATE memory.owners,memory.jobs,memory.audit,memory.operations CASCADE");
    sql.update(
        "UPDATE memory.privacy_ledger_binding SET lineage=?,active=false",
        UUID.randomUUID().toString());
    external.execute("TRUNCATE privacy_ledger.forget_facts,privacy_ledger.lineages");
  }

  @Test
  void missingIsolationAcknowledgementExitsBeforeOpeningOrWritingDatabases() throws Exception {
    var response = launch(List.of(action("bootstrap")), Map.of());
    blocked(response);
    assertThat(active()).isFalse();
    assertThat(count(sql, "memory.owners")).isZero();
    assertThat(count(external, "privacy_ledger.lineages")).isZero();
  }

  @Test
  void packagedBootstrapAndEmptyReplayReturnOnlySafeReports() throws Exception {
    var bootstrap = launch(List.of(ISOLATED, action("bootstrap")), Map.of());
    report(bootstrap, "privacy_ledger_initialized", 0, 0);
    assertThat(active()).isTrue();
    assertThat(count(external, "privacy_ledger.lineages")).isEqualTo(1);
    assertThat(count(external, "privacy_ledger.forget_facts")).isZero();
    var replay = launch(List.of(ISOLATED, action("replay")), Map.of());
    report(replay, "privacy_replayed", 0, 0);
    assertThat(replay.json().get("fingerprint").asText())
        .isEqualTo(bootstrap.json().get("fingerprint").asText());
  }

  @Test
  void packagedReplayFinishesAuthorizedPendingRequestAndRetryKeepsTheSameReceipt()
      throws Exception {
    report(
        launch(List.of(ISOLATED, action("bootstrap")), Map.of()),
        "privacy_ledger_initialized",
        0,
        0);
    String event = UUID.randomUUID().toString();
    String conversation = UUID.randomUUID().toString();
    String turn = UUID.randomUUID().toString();
    String lineage =
        sql.queryForObject("SELECT lineage FROM memory.privacy_ledger_binding", String.class);
    sql.update(
        "INSERT INTO memory.owners(tenant,subject,epoch,purge_pending,profile_revision) VALUES(?,?,1,true,3)",
        TENANT,
        OWNER);
    sql.update(
        "INSERT INTO memory.user_profiles(tenant,subject,epoch,revision,markdown) VALUES(?,?,1,3,?)",
        TENANT,
        OWNER,
        CONTENT);
    sql.update(
        "INSERT INTO business.conversations(id,tenant,subject) VALUES(?,?,?)",
        conversation,
        TENANT,
        OWNER);
    sql.update(
        "INSERT INTO business.turns(id,conversation_id,tenant,subject,idempotency_key,"
            + "request_hash,message,release_hash,status) VALUES(?,?,?,?,?,?,?,?,?)",
        turn,
        conversation,
        TENANT,
        OWNER,
        "old-turn",
        "test-hash",
        CONTENT,
        "test-release",
        "completed");
    sql.update(
        "INSERT INTO memory.sources(id,tenant,subject,epoch,content,status)"
            + " VALUES(?,?,?,1,?,'ready')",
        turn,
        TENANT,
        OWNER,
        CONTENT);
    sql.update(
        "INSERT INTO memory.jobs(kind,key_id,tenant,subject,epoch) VALUES('extract',?,?,?,1)",
        turn,
        TENANT,
        OWNER);
    sql.update(
        "INSERT INTO memory.forget_requests(tenant,subject,event_id,lineage,from_epoch,"
            + "operation_key,request_hash) VALUES(?,?,?,?,1,?,?)",
        TENANT,
        OWNER,
        event,
        lineage,
        "authorized-before-recovery",
        ForgetFact.hash(1));

    var first =
        launch(
            List.of(ISOLATED, action("replay")),
            Map.of(
                "SPRING_APPLICATION_JSON",
                "{\"logging\":{\"level\":{\"org.springframework.jdbc.core\":\"TRACE\",\"com.zaxxer.hikari\":\"DEBUG\"}}}"));
    report(first, "privacy_replayed", 1, 1);
    assertThat(sql.queryForObject("SELECT epoch FROM memory.owners", Long.class)).isEqualTo(2);
    assertThat(sql.queryForObject("SELECT profile_revision FROM memory.owners", Long.class))
        .isZero();
    assertThat(count(sql, "memory.user_profiles")).isZero();
    assertThat(sql.queryForObject("SELECT purge_pending FROM memory.owners", Boolean.class))
        .isFalse();
    assertThat(sql.queryForObject("SELECT content FROM memory.sources", String.class)).isNull();
    assertThat(sql.queryForObject("SELECT status FROM memory.sources", String.class))
        .isEqualTo("forgotten");
    assertThat(sql.queryForObject("SELECT status FROM memory.jobs", String.class))
        .isEqualTo("forgotten");
    assertThat(count(sql, "memory.forget_requests")).isZero();
    assertThat(count(sql, "memory.forget_applied")).isEqualTo(1);
    assertThat(count(sql, "memory.audit")).isEqualTo(2);
    assertThat(sql.queryForObject("SELECT result FROM memory.operations", String.class))
        .isEqualTo("2");
    assertThat(count(external, "privacy_ledger.forget_facts")).isEqualTo(1);
    // Business history is outside the memory erasure contract.
    assertThat(
            sql.queryForObject("SELECT message FROM business.turns", String.class).equals(CONTENT))
        .isTrue();

    var retried = launch(List.of(ISOLATED, action("replay")), Map.of());
    report(retried, "privacy_replayed", 1, 1);
    assertThat(retried.json()).isEqualTo(first.json());
    assertThat(count(sql, "memory.audit")).isEqualTo(2);
    assertThat(count(sql, "memory.operations")).isEqualTo(1);
    assertThat(count(external, "privacy_ledger.forget_facts")).isEqualTo(1);
  }

  @Test
  void invalidConnectionConfigurationReturnsFixedErrorWithoutExposingConfiguredValues()
      throws Exception {
    var response =
        launch(
            List.of(ISOLATED, action("bootstrap")),
            Map.of("FINANCECLAW_FORGET_LEDGER_URL", "jdbc:unsupported:" + OWNER));
    blocked(response);
    assertThat(active()).isFalse();
    assertThat(count(external, "privacy_ledger.lineages")).isZero();
  }

  @Test
  void sqlFailureReturnsFixedErrorWithoutPrintingSqlExceptionOrSensitiveParameters()
      throws Exception {
    sql.execute(
        "CREATE FUNCTION memory.reject_cli_bootstrap() RETURNS trigger LANGUAGE plpgsql AS $$"
            + " BEGIN RAISE EXCEPTION 'private SQL detail: "
            + OWNER
            + " "
            + MAIN_PASSWORD
            + "'; END $$");
    sql.execute(
        "CREATE TRIGGER reject_cli_bootstrap BEFORE UPDATE ON memory.privacy_ledger_binding"
            + " FOR EACH ROW EXECUTE FUNCTION memory.reject_cli_bootstrap()");
    try {
      blocked(launch(List.of(ISOLATED, action("bootstrap")), Map.of()));
      assertThat(active()).isFalse();
    } finally {
      sql.execute("DROP TRIGGER reject_cli_bootstrap ON memory.privacy_ledger_binding");
      sql.execute("DROP FUNCTION memory.reject_cli_bootstrap()");
    }
  }

  @ParameterizedTest
  @ValueSource(
      strings = {
        "--spring.main.web-application-type=servlet",
        "--spring.flyway.enabled=true",
        "--logging.level.root=DEBUG",
        "--financeclaw.privacy-recovery.isolated=false",
        "--financeclaw.privacy-recovery.action=replay"
      })
  void overridesAndRepeatedFlagsAreRejectedBeforeHttpOrFlywayCanStart(String override)
      throws Exception {
    blocked(
        launch(
            List.of(ISOLATED, action("bootstrap"), override),
            Map.of("FINANCECLAW_DATABASE_URL", unmigratedUrl)));
    assertThat(
            unmigrated.queryForObject(
                "SELECT count(*) FROM pg_catalog.pg_class c"
                    + " JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace"
                    + " WHERE n.nspname IN ('business','memory','privacy_ledger')"
                    + " OR (n.nspname='public' AND c.relname='flyway_schema_history')",
                Long.class))
        .isZero();
    assertThat(count(external, "privacy_ledger.lineages")).isZero();
    assertThat(active()).isFalse();
  }

  private CliResponse launch(List<String> arguments, Map<String, String> overrides)
      throws Exception {
    Path application = Path.of("target/financeclaw-java-0.1.0-SNAPSHOT.jar").toAbsolutePath();
    assertThat(Files.isRegularFile(application)).as("repackaged application exists").isTrue();
    var command =
        new ArrayList<>(
            List.of(
                Path.of(System.getProperty("java.home"), "bin/java").toString(),
                "-Dfile.encoding=UTF-8",
                "-Dloader.main=com.financeclawprivacy.PrivacyRecoveryApplication",
                "-cp",
                application.toString(),
                "org.springframework.boot.loader.launch.PropertiesLauncher"));
    command.addAll(arguments);
    Path outputFile =
        Files.createTempFile(
            directory,
            "private-cli-",
            ".log",
            PosixFilePermissions.asFileAttribute(PosixFilePermissions.fromString("rw-------")));
    var builder =
        new ProcessBuilder(command)
            .directory(directory.toFile())
            .redirectErrorStream(true)
            .redirectOutput(outputFile.toFile());
    var env = builder.environment();
    env.clear();
    env.put("PATH", "");
    env.put("FINANCECLAW_DATABASE_URL", MAIN.getJdbcUrl());
    env.put("FINANCECLAW_DATABASE_USER", MAIN.getUsername());
    env.put("FINANCECLAW_DATABASE_PASSWORD", MAIN_PASSWORD);
    env.put("FINANCECLAW_FORGET_LEDGER_ENABLED", "true");
    env.put("FINANCECLAW_FORGET_LEDGER_URL", LEDGER.getJdbcUrl());
    env.put("FINANCECLAW_FORGET_LEDGER_USER", LEDGER.getUsername());
    env.put("FINANCECLAW_FORGET_LEDGER_PASSWORD", LEDGER_PASSWORD);
    env.putAll(overrides);
    var process = builder.start();
    try {
      assertThat(process.waitFor(30, TimeUnit.SECONDS)).as("offline CLI terminates").isTrue();
      String output = Files.readString(outputFile).strip();
      Files.delete(outputFile);
      for (String forbidden :
          List.of(
              MAIN_PASSWORD,
              LEDGER_PASSWORD,
              MAIN.getJdbcUrl(),
              LEDGER.getJdbcUrl(),
              MAIN.getUsername(),
              LEDGER.getUsername(),
              TENANT,
              OWNER,
              CONTENT,
              "Exception",
              "Tomcat",
              "Hikari",
              "Started "))
        assertThat(output.contains(forbidden))
            .as("CLI output contains no sensitive values or runtime logs")
            .isFalse();
      assertThat(output.lines().count()).as("exactly one JSON output line").isEqualTo(1);
      JsonNode json;
      try {
        json = new ObjectMapper().readTree(output);
      } catch (Exception malformed) {
        throw new AssertionError("CLI output is not a safe JSON report");
      }
      assertThat(json != null && json.isObject()).as("CLI JSON report is an object").isTrue();
      return new CliResponse(process.exitValue(), json);
    } finally {
      process.destroyForcibly();
      Files.deleteIfExists(outputFile);
    }
  }

  private static String action(String action) {
    return "--financeclaw.privacy-recovery.action=" + action;
  }

  private static void blocked(CliResponse response) {
    assertThat(response.exit()).isEqualTo(2);
    assertThat(fields(response.json()).equals(Set.of("result", "error"))).isTrue();
    assertThat("blocked".equals(response.json().path("result").asText())).isTrue();
    assertThat(
            Set.of("isolation_required", "invalid_recovery_arguments", "privacy_recovery_failed")
                .contains(response.json().path("error").asText()))
        .isTrue();
  }

  private static void report(CliResponse response, String result, int facts, int owners) {
    assertThat(response.exit()).isZero();
    assertThat(fields(response.json()).equals(Set.of("result", "facts", "owners", "fingerprint")))
        .isTrue();
    assertThat(result.equals(response.json().path("result").asText())).isTrue();
    assertThat(response.json().path("facts").asInt()).isEqualTo(facts);
    assertThat(response.json().path("owners").asInt()).isEqualTo(owners);
    assertThat(response.json().path("fingerprint").asText().matches("[0-9a-f]{64}")).isTrue();
  }

  private static Set<String> fields(JsonNode json) {
    var fields = new HashSet<String>();
    json.fieldNames().forEachRemaining(fields::add);
    return fields;
  }

  private static boolean active() {
    return Boolean.TRUE.equals(
        sql.queryForObject("SELECT active FROM memory.privacy_ledger_binding", Boolean.class));
  }

  private static long count(JdbcTemplate database, String table) {
    return database.queryForObject("SELECT count(*) FROM " + table, Long.class);
  }

  private record CliResponse(int exit, JsonNode json) {}
}
