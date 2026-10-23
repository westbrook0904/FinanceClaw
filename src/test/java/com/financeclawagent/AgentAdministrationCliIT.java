package com.financeclawagent;

import static org.assertj.core.api.Assertions.*;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.financeclaw.support.PayloadJson;
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

/** 真实打包 JVM 和隔离 PostgreSQL：CLI 发布、owner 隔离、无自动迁移及安全输出。 */
class AgentAdministrationCliIT {
    private static final String ID = "19a37cb0-fd86-4b59-8bdb-405150f32fe0";
    private static final String SECOND_ID = "a5af8e40-0bc8-4a2b-9818-917fc4a89034";
    private static final String TENANT = "agent-tenant-private-sentinel";
    private static final String SUBJECT = "agent-subject-private-sentinel";
    private static final String OTHER_SUBJECT = "agent-other-owner-private-sentinel";
    private static final String PASSWORD = "agent-cli-password-private-sentinel";
    private static final String MODEL_KEY = "agent-cli-model-key-private-sentinel";
    private static final String PROMPT = "agent-prompt-private-sentinel";
    private static final String NAME = "agent-name-private-sentinel";
    private static final PostgreSQLContainer<?> DATABASE =
            new PostgreSQLContainer<>("postgres:16.10-alpine")
                    .withDatabaseName("agent_cli")
                    .withUsername("agent_cli_user")
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
        sql.execute("CREATE DATABASE agent_cli_unmigrated");
        unmigratedUrl = DATABASE.getJdbcUrl().replace("/agent_cli", "/agent_cli_unmigrated");
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
        sql.execute("TRUNCATE control_plane.agents, control_plane.release_manifests CASCADE");
    }

    @Test
    void packagedCreateUpdatePublishAndHistoryPreservePinnedBaseAndExplicitCas() throws Exception {
        String defaultHash = bootstrap();
        Path file = definition(PROMPT);
        var created = launch(options("create", ID, "--file", file.toString()), Map.of());
        assertThat(created.exit()).isZero();
        assertThat(created.json().path("draftRevision").asLong()).isEqualTo(1);
        assertThat(created.json().path("baseReleaseHash").asText()).isEqualTo(defaultHash);
        assertThat(created.json().path("inputSha256").asText()).matches("[0-9a-f]{64}");
        var duplicate = launch(options("create", ID, "--file", file.toString()), Map.of());
        assertThat(duplicate.exit()).isZero();
        assertThat(duplicate.json()).isEqualTo(created.json());

        var first =
                launch(
                        options("publish", ID, "--draft-revision", "1", "--expected-revision", "0"),
                        Map.of());
        assertThat(first.exit()).isZero();
        assertThat(first.json().path("revision").asLong()).isEqualTo(1);
        String firstHash = first.json().path("releaseHash").asText();
        assertThat(firstHash).matches("[0-9a-f]{64}").isNotEqualTo(defaultHash);
        assertDefault(defaultHash);

        definition(PROMPT + " revised");
        var updated =
                launch(
                        options(
                                "update",
                                ID,
                                "--file",
                                file.toString(),
                                "--expected-revision",
                                "1"),
                        Map.of());
        assertThat(updated.exit()).isZero();
        assertThat(updated.json().path("draftRevision").asLong()).isEqualTo(2);
        assertThat(updated.json().path("contentHash").asText())
                .isNotEqualTo(created.json().path("contentHash").asText());
        assertBlocked(
                launch(
                        options(
                                "update",
                                ID,
                                "--file",
                                file.toString(),
                                "--expected-revision",
                                "1"),
                        Map.of()),
                "agent_draft_revision_changed");
        var beforePublish = launch(options("history", ID), Map.of());
        assertThat(beforePublish.json().path("items").size()).isEqualTo(1);
        assertThat(beforePublish.json().path("items").get(0).path("releaseHash").asText())
                .isEqualTo(firstHash);
        assertBlocked(
                launch(
                        options("publish", ID, "--draft-revision", "1", "--expected-revision", "1"),
                        Map.of()),
                "agent_draft_revision_changed");
        var second =
                launch(
                        options("publish", ID, "--draft-revision", "2", "--expected-revision", "1"),
                        Map.of());
        assertThat(second.exit()).isZero();
        assertThat(second.json().path("revision").asLong()).isEqualTo(2);
        assertThat(second.json().path("releaseHash").asText()).isNotEqualTo(firstHash);
        var history =
                launch(options("history", ID, "--after-revision", "1", "--limit", "1"), Map.of());
        assertThat(history.json().path("items").size()).isEqualTo(1);
        assertThat(history.json().path("nextAfterRevision").asLong()).isEqualTo(2);
        assertThat(history.json().path("items").get(0).path("previousHash").asText())
                .isEqualTo(firstHash);
        assertDefault(defaultHash);
        assertThat(sql.queryForObject("SELECT count(*) FROM business.turns", Long.class)).isZero();
        assertThat(sql.queryForObject("SELECT count(*) FROM business.commands", Long.class))
                .isZero();
    }

    @Test
    void ownerScopedReadsAndIndependentAgentPointersWorkWithInvalidEnvironmentCandidate()
            throws Exception {
        String defaultHash = bootstrap();
        Path file = definition(PROMPT);
        var badCandidate = Map.of("FINANCECLAW_MODEL_INPUT_LIMIT", "0");
        assertThat(launch(options("create", ID, "--file", file.toString()), badCandidate).exit())
                .isZero();
        var otherCreate = new ArrayList<>(options("create", SECOND_ID, "--file", file.toString()));
        otherCreate.set(otherCreate.indexOf("--subject") + 1, OTHER_SUBJECT);
        assertThat(launch(otherCreate, badCandidate).exit()).isZero();
        assertThat(
                        launch(
                                        options(
                                                "publish",
                                                ID,
                                                "--draft-revision",
                                                "1",
                                                "--expected-revision",
                                                "0"),
                                        badCandidate)
                                .exit())
                .isZero();
        var otherPublish =
                new ArrayList<>(
                        options(
                                "publish",
                                SECOND_ID,
                                "--draft-revision",
                                "1",
                                "--expected-revision",
                                "0"));
        otherPublish.set(otherPublish.indexOf("--subject") + 1, OTHER_SUBJECT);
        var otherPublished = launch(otherPublish, badCandidate);
        assertThat(otherPublished.exit()).isZero();
        assertThat(otherPublished.json().path("revision").asLong()).isEqualTo(1);
        assertBlocked(launch(options("get", SECOND_ID), badCandidate), "not_found");
        assertBlocked(
                launch(
                        options(
                                "publish",
                                SECOND_ID,
                                "--draft-revision",
                                "1",
                                "--expected-revision",
                                "0"),
                        badCandidate),
                "not_found");
        var list = launch(List.of("list", "--tenant", TENANT, "--subject", SUBJECT), badCandidate);
        assertThat(list.exit()).isZero();
        assertThat(list.json().toString()).contains(ID).doesNotContain(SECOND_ID);
        var builtin = launch(options("get", "finance_agent"), badCandidate);
        assertThat(builtin.exit()).isZero();
        assertThat(builtin.json().path("draftRevision").asLong()).isZero();
        assertBlocked(
                launch(
                        options(
                                "update",
                                "finance_agent",
                                "--file",
                                file.toString(),
                                "--expected-revision",
                                "1"),
                        badCandidate),
                "agent_read_only");
        var history = launch(options("history", ID), badCandidate);
        assertThat(history.exit()).isZero();
        assertThat(history.json().path("items").size()).isEqualTo(1);
        assertDefault(defaultHash);
    }

    @Test
    void packagedCompositionPublishAndFreshProcessReadKeepFrozenPlan() throws Exception {
        String defaultHash = bootstrap();
        Path file = definition(PROMPT);
        Files.writeString(
                file,
                PayloadJson.canonical(
                        Map.of(
                                "displayName",
                                NAME,
                                "systemPrompt",
                                PROMPT,
                                "composition",
                                Map.of(
                                        "modelProfile",
                                        "primary",
                                        "capabilities",
                                        List.of("function:request_city")))));
        var created = launch(options("create", ID, "--file", file.toString()), Map.of());
        assertThat(created.exit()).isZero();
        var catalog = launch(options("catalog", ID), Map.of());
        assertThat(catalog.exit()).isZero();
        assertThat(catalog.json().path("catalog").path("capabilities").toString())
                .contains("function:request_city");
        var published =
                launch(
                        options("publish", ID, "--draft-revision", "1", "--expected-revision", "0"),
                        Map.of());
        assertThat(published.exit()).isZero();
        String hash = published.json().path("releaseHash").asText();
        var record =
                sql.queryForMap(
                        "SELECT manifest_format, manifest::text AS body FROM control_plane.release_manifests WHERE release_hash=?",
                        hash);
        assertThat(record.get("manifest_format")).isEqualTo("agent-v3");
        var body = new ObjectMapper().readTree((String) record.get("body"));
        assertThat(body.path("plan").path("runtimeArtifactHash").asText())
                .isEqualTo(com.financeclaw.infrastructure.agentscope.RuntimeArtifacts.hash());
        assertThat(body.path("plan").path("capabilities").size()).isEqualTo(2);
        assertThat(body.path("plan").path("requestedCapabilities").get(0).asText())
                .isEqualTo("function:request_city");
        // A legacy two-field edit retains the existing composition; it cannot broaden to
        // inheritance.
        definition(PROMPT + " revised");
        var updated =
                launch(
                        options(
                                "update",
                                ID,
                                "--file",
                                file.toString(),
                                "--expected-revision",
                                "1"),
                        Map.of());
        assertThat(updated.exit()).isZero();
        assertThat(
                        sql.queryForObject(
                                "SELECT composition->>'modelProfile' FROM control_plane.agent_draft_revisions WHERE agent_id=? AND revision=2",
                                String.class,
                                ID))
                .isEqualTo("primary");
        assertThat(
                        launch(options("history", ID), Map.of())
                                .json()
                                .path("items")
                                .get(0)
                                .path("releaseHash")
                                .asText())
                .isEqualTo(hash);
        assertDefault(defaultHash);
    }

    @Test
    void invalidDefinitionAndFrameworkOverridesNeverInitializeDatabase() throws Exception {
        Path file = directory.resolve("invalid-definition.json");
        Files.writeString(
                file,
                "{\"displayName\":\"name\",\"systemPrompt\":\"prompt\",\"baseReleaseHash\":\"not-allowed\"}");
        var unmigratedDatabase = Map.of("FINANCECLAW_DATABASE_URL", unmigratedUrl);
        assertBlocked(
                launch(options("create", ID, "--file", file.toString()), unmigratedDatabase),
                "invalid_agent_definition");
        assertBlocked(
                launch(options("get", ID, "--spring.flyway.enabled", "true"), unmigratedDatabase),
                "invalid_agent_admin_arguments");
        var response =
                launch(
                        options("get", ID),
                        Map.of(
                                "FINANCECLAW_DATABASE_URL",
                                unmigratedUrl,
                                "SPRING_APPLICATION_JSON",
                                "{\"spring\":{\"main\":{\"web-application-type\":\"servlet\"},\"flyway\":{\"enabled\":true}},\"logging\":{\"level\":{\"root\":\"DEBUG\"}}}"));
        assertBlocked(response, "agent_admin_failed");
        assertThat(
                        unmigrated.queryForObject(
                                "SELECT count(*) FROM pg_catalog.pg_namespace WHERE nspname IN ('business','agent_runtime','memory','control_plane')",
                                Long.class))
                .isZero();
    }

    @Test
    void invalidConnectionReturnsOnlySanitizedFailure() throws Exception {
        assertBlocked(
                launch(
                        options("get", ID),
                        Map.of("FINANCECLAW_DATABASE_URL", "jdbc:unsupported:" + MODEL_KEY)),
                "agent_admin_failed");
    }

    private String bootstrap() throws Exception {
        var response =
                launch(
                        "com.financeclawrelease.ReleaseAdministrationApplication",
                        List.of("prepare"),
                        Map.of());
        assertThat(response.exit()).isZero();
        return response.json().path("candidateHash").asText();
    }

    private Path definition(String prompt) throws Exception {
        Path file = directory.resolve("definition.json");
        Files.writeString(
                file, PayloadJson.canonical(Map.of("displayName", NAME, "systemPrompt", prompt)));
        return file;
    }

    private CliResponse launch(List<String> arguments, Map<String, String> overrides)
            throws Exception {
        return launch("com.financeclawagent.AgentAdministrationApplication", arguments, overrides);
    }

    private CliResponse launch(String main, List<String> arguments, Map<String, String> overrides)
            throws Exception {
        Path application = Path.of("target/financeclaw-java-0.1.0-SNAPSHOT.jar").toAbsolutePath();
        assertThat(Files.isRegularFile(application)).as("repackaged application exists").isTrue();
        var command =
                new ArrayList<>(
                        List.of(
                                Path.of(System.getProperty("java.home"), "bin/java").toString(),
                                "-Dfile.encoding=UTF-8",
                                "-Dloader.main=" + main,
                                "-cp",
                                application.toString(),
                                "org.springframework.boot.loader.launch.PropertiesLauncher"));
        command.addAll(arguments);
        Path outputFile =
                Files.createTempFile(
                        directory,
                        "agent-cli-",
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
                            TENANT,
                            SUBJECT,
                            OTHER_SUBJECT,
                            PROMPT,
                            NAME,
                            DATABASE.getJdbcUrl(),
                            DATABASE.getUsername(),
                            "Exception",
                            "Tomcat",
                            "Hikari",
                            "Started ",
                            "\"manifest\"",
                            "\"systemPrompt\"",
                            "\"displayName\"")) {
                assertThat(output.contains(forbidden))
                        .as("output excludes private content and framework logs")
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

    private static List<String> options(String command, String id, String... extra) {
        var result =
                new ArrayList<>(
                        List.of(
                                command,
                                "--tenant",
                                TENANT,
                                "--subject",
                                SUBJECT,
                                "--agent-id",
                                id));
        result.addAll(List.of(extra));
        return result;
    }

    private static void assertDefault(String hash) {
        assertThat(
                        sql.queryForObject(
                                "SELECT release_hash FROM control_plane.deployments WHERE name='default'",
                                String.class))
                .isEqualTo(hash);
        assertThat(
                        sql.queryForObject(
                                "SELECT revision FROM control_plane.deployments WHERE name='default'",
                                Long.class))
                .isEqualTo(1);
    }

    private static void assertBlocked(CliResponse response, String error) {
        assertThat(response.exit()).isEqualTo(2);
        assertThat(response.json().size()).isEqualTo(2);
        assertThat(response.json().path("result").asText()).isEqualTo("blocked");
        assertThat(response.json().path("error").asText()).isEqualTo(error);
    }

    private record CliResponse(int exit, JsonNode json) {}
}
