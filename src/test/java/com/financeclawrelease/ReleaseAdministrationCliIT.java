package com.financeclawrelease;

import static org.assertj.core.api.Assertions.*;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.attribute.PosixFilePermissions;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.TimeUnit;
import org.flywaydb.core.Flyway;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DriverManagerDataSource;
import org.testcontainers.containers.PostgreSQLContainer;

/** 打包后的运维入口在独立 JVM 中连接真实 PostgreSQL，验证显式 CAS 和安全输出。 */
class ReleaseAdministrationCliIT {
    private static final String PASSWORD = "release-cli-password-private-sentinel";
    private static final String MODEL_KEY = "release-cli-model-key-private-sentinel";
    private static final PostgreSQLContainer<?> DATABASE =
            new PostgreSQLContainer<>("postgres:16.10-alpine")
                    .withDatabaseName("release_cli")
                    .withUsername("release_cli_user")
                    .withPassword(PASSWORD);
    private static JdbcTemplate sql;
    private static JdbcTemplate unmigrated;
    private static String unmigratedUrl;
    @TempDir Path directory;

    @BeforeAll
    static void prepareDatabase() {
        DATABASE.start();
        var source =
                new DriverManagerDataSource(
                        DATABASE.getJdbcUrl(), DATABASE.getUsername(), PASSWORD);
        Flyway.configure().dataSource(source).load().migrate();
        sql = new JdbcTemplate(source);
        sql.execute("CREATE DATABASE release_cli_unmigrated");
        unmigratedUrl = DATABASE.getJdbcUrl().replace("/release_cli", "/release_cli_unmigrated");
        unmigrated =
                new JdbcTemplate(
                        new DriverManagerDataSource(
                                unmigratedUrl, DATABASE.getUsername(), PASSWORD));
    }

    @AfterAll
    static void stopDatabase() {
        DATABASE.stop();
    }

    @BeforeEach
    void resetCatalog() {
        sql.execute("TRUNCATE control_plane.release_manifests CASCADE");
    }

    @Test
    void packagedCommandsPreserveCandidateActivateByCasAndPageRollbackHistory() throws Exception {
        var initial = launch(List.of("prepare"), Map.of());
        assertThat(initial.exit()).isZero();
        String firstHash = initial.json().path("candidateHash").asText();
        assertThat(firstHash).matches("[0-9a-f]{64}");
        assertCurrent(initial, firstHash, 1);

        var candidate =
                launch(List.of("prepare"), Map.of("FINANCECLAW_MODEL_INPUT_LIMIT", "64000"));
        String secondHash = candidate.json().path("candidateHash").asText();
        assertThat(secondHash).matches("[0-9a-f]{64}").isNotEqualTo(firstHash);
        assertCurrent(candidate, firstHash, 1);
        assertThat(count("control_plane.release_manifests")).isEqualTo(2);
        assertThat(count("control_plane.deployment_changes")).isEqualTo(1);

        var activated = launch(activate(secondHash, 1), Map.of());
        assertThat(activated.exit()).isZero();
        assertCurrent(activated, secondHash, 2);
        var stale = launch(activate(firstHash, 1), Map.of());
        assertBlocked(stale, "release_revision_changed");
        assertThat(count("control_plane.deployment_changes")).isEqualTo(2);

        var rollback = launch(activate(firstHash, 2), Map.of());
        assertThat(rollback.exit()).isZero();
        assertCurrent(rollback, firstHash, 3);
        var firstPage = launch(List.of("history", "--limit", "1"), Map.of());
        assertThat(firstPage.exit()).isZero();
        assertThat(firstPage.json().path("items").size()).isEqualTo(1);
        assertThat(firstPage.json().path("nextAfterRevision").asLong()).isEqualTo(1);
        var nextPage =
                launch(List.of("history", "--after-revision", "1", "--limit", "2"), Map.of());
        assertThat(nextPage.json().path("items").size()).isEqualTo(2);
        assertThat(nextPage.json().path("nextAfterRevision").asLong()).isEqualTo(3);
        assertThat(nextPage.json().path("items").get(1).path("previousHash").asText())
                .isEqualTo(secondHash);
        assertThat(count("business.turns")).isZero();
        assertThat(count("business.commands")).isZero();
    }

    @Test
    void invalidEnvironmentCandidateDoesNotPreventHistoryOrActivation() throws Exception {
        var initial = launch(List.of("prepare"), Map.of());
        assertThat(initial.exit()).isZero();
        String firstHash = initial.json().path("candidateHash").asText();
        var candidate =
                launch(List.of("prepare"), Map.of("FINANCECLAW_MODEL_INPUT_LIMIT", "64000"));
        assertThat(candidate.exit()).isZero();
        String secondHash = candidate.json().path("candidateHash").asText();
        assertThat(secondHash).isNotEqualTo(firstHash);
        var invalidEnvironment = Map.of("FINANCECLAW_MODEL_INPUT_LIMIT", "0");

        var history = launch(List.of("history"), invalidEnvironment);
        assertThat(history.exit()).isZero();
        assertThat(history.json().path("items").size()).isEqualTo(1);
        var activated = launch(activate(secondHash, 1), invalidEnvironment);
        assertThat(activated.exit()).isZero();
        assertCurrent(activated, secondHash, 2);

        var rejected = launch(List.of("prepare"), invalidEnvironment);
        assertBlocked(rejected, "release_admin_failed");
        assertThat(
                        sql.queryForObject(
                                "SELECT release_hash FROM control_plane.deployments WHERE name='default'",
                                String.class))
                .isEqualTo(secondHash);
        assertThat(
                        sql.queryForObject(
                                "SELECT revision FROM control_plane.deployments WHERE name='default'",
                                Long.class))
                .isEqualTo(2);
        assertThat(count("control_plane.release_manifests")).isEqualTo(2);
        assertThat(count("control_plane.deployment_changes")).isEqualTo(2);
    }

    @Test
    void rejectsFrameworkOverridesBeforeDatabaseInitialization() throws Exception {
        var response =
                launch(
                        List.of("prepare", "--spring.flyway.enabled=true"),
                        Map.of("FINANCECLAW_DATABASE_URL", unmigratedUrl));
        assertBlocked(response, "invalid_release_admin_arguments");
        assertUnmigrated();
    }

    @Test
    void configuredFlywayAndHttpCannotTurnTheOneShotCommandIntoAnApplication() throws Exception {
        var response =
                launch(
                        List.of("prepare"),
                        Map.of(
                                "FINANCECLAW_DATABASE_URL",
                                unmigratedUrl,
                                "SPRING_APPLICATION_JSON",
                                "{\"spring\":{\"main\":{\"web-application-type\":\"servlet\"},"
                                        + "\"flyway\":{\"enabled\":true}},"
                                        + "\"logging\":{\"level\":{\"root\":\"DEBUG\"}}}"));
        assertBlocked(response, "release_admin_failed");
        assertUnmigrated();
    }

    @Test
    void invalidDatabaseConfigurationNeverPrintsTheConnectionOrSecret() throws Exception {
        var response =
                launch(
                        List.of("history"),
                        Map.of("FINANCECLAW_DATABASE_URL", "jdbc:unsupported:" + MODEL_KEY));
        assertBlocked(response, "release_admin_failed");
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
                                "-Dloader.main=com.financeclawrelease.ReleaseAdministrationApplication",
                                "-cp",
                                application.toString(),
                                "org.springframework.boot.loader.launch.PropertiesLauncher"));
        command.addAll(arguments);
        Path outputFile =
                Files.createTempFile(
                        directory,
                        "release-cli-",
                        ".log",
                        PosixFilePermissions.asFileAttribute(
                                PosixFilePermissions.fromString("rw-------")));
        var builder =
                new ProcessBuilder(command)
                        .directory(directory.toFile())
                        .redirectErrorStream(true)
                        .redirectOutput(outputFile.toFile());
        var environment = builder.environment();
        environment.clear();
        environment.put("PATH", "");
        environment.put("FINANCECLAW_DATABASE_URL", DATABASE.getJdbcUrl());
        environment.put("FINANCECLAW_DATABASE_USER", DATABASE.getUsername());
        environment.put("FINANCECLAW_DATABASE_PASSWORD", PASSWORD);
        environment.put("FINANCECLAW_MODEL_API_KEY", MODEL_KEY);
        environment.put("FINANCECLAW_WORKER_ENABLED", "true");
        environment.put("FINANCECLAW_FEISHU_ENABLED", "true");
        environment.putAll(overrides);
        var process = builder.start();
        try {
            assertThat(process.waitFor(30, TimeUnit.SECONDS)).as("CLI terminates").isTrue();
            String output = Files.readString(outputFile).strip();
            for (String forbidden :
                    List.of(
                            PASSWORD,
                            MODEL_KEY,
                            DATABASE.getJdbcUrl(),
                            DATABASE.getUsername(),
                            "Exception",
                            "Tomcat",
                            "Hikari",
                            "Started ",
                            "\"manifest\"")) {
                assertThat(output.contains(forbidden))
                        .as("output excludes sensitive configuration and framework logs")
                        .isFalse();
            }
            assertThat(output.lines().count()).as("one structured result").isEqualTo(1);
            JsonNode json;
            try {
                json = new ObjectMapper().readTree(output);
            } catch (Exception invalid) {
                throw new AssertionError("CLI output is not a safe JSON report");
            }
            assertThat(json != null && json.isObject()).isTrue();
            return new CliResponse(process.exitValue(), json);
        } finally {
            process.destroyForcibly();
            Files.deleteIfExists(outputFile);
        }
    }

    private static List<String> activate(String hash, long revision) {
        return List.of("activate", "--hash", hash, "--expected-revision", Long.toString(revision));
    }

    private static void assertCurrent(CliResponse response, String hash, long revision) {
        assertThat(response.json().path("current").path("releaseHash").asText()).isEqualTo(hash);
        assertThat(response.json().path("current").path("revision").asLong()).isEqualTo(revision);
    }

    private static void assertBlocked(CliResponse response, String error) {
        assertThat(response.exit()).isEqualTo(2);
        assertThat(response.json().size()).isEqualTo(2);
        assertThat(response.json().path("result").asText()).isEqualTo("blocked");
        assertThat(response.json().path("error").asText()).isEqualTo(error);
    }

    private static void assertUnmigrated() {
        assertThat(
                        unmigrated.queryForObject(
                                "SELECT count(*) FROM pg_catalog.pg_namespace WHERE nspname IN"
                                        + " ('business','agent_runtime','memory','control_plane')",
                                Long.class))
                .isZero();
    }

    private static long count(String table) {
        return sql.queryForObject("SELECT count(*) FROM " + table, Long.class);
    }

    private record CliResponse(int exit, JsonNode json) {}
}
