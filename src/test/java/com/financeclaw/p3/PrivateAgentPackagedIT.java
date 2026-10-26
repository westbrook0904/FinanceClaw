package com.financeclaw.p3;

import static org.assertj.core.api.Assertions.*;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.net.URI;
import java.net.http.*;
import java.nio.file.*;
import java.security.MessageDigest;
import java.time.Duration;
import java.util.*;
import java.util.concurrent.TimeUnit;
import java.util.regex.Pattern;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import org.testcontainers.containers.PostgreSQLContainer;

/**
 * Starts only the executable application JAR: no test launcher or test classes enter its classpath.
 */
class PrivateAgentPackagedIT {
    @TempDir Path directory;
    private final ObjectMapper json = new ObjectMapper();
    private final HttpClient http =
            HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(5)).build();
    private static final String TOKEN = "synthetic-packaged-manager-token";
    private Path jar = Path.of("target/financeclaw-java-0.1.0-SNAPSHOT.jar").toAbsolutePath();
    private Map<String, String> environment;
    private String api;

    @Test
    void separateJarExecutesFrozenMarketChildAcrossIndependentReleaseChanges() throws Exception {
        try (var pg = new PostgreSQLContainer<>("postgres:16.10-alpine");
                var model = new PrivateAgentLoopback()) {
            pg.start();
            Path marketA = directory.resolve("market-a.json"),
                    marketB = directory.resolve("market-b.json");
            json.writeValue(
                    marketA.toFile(),
                    Map.of(
                            "AAPL",
                            Map.of(
                                    "price",
                                    "101.11",
                                    "currency",
                                    "USD",
                                    "provider",
                                    "PACKAGED_TARGET_A",
                                    "as_of",
                                    "2026-10-20T01:02:03Z")));
            json.writeValue(
                    marketB.toFile(),
                    Map.of(
                            "AAPL",
                            Map.of(
                                    "price",
                                    "303.33",
                                    "currency",
                                    "USD",
                                    "provider",
                                    "PACKAGED_PARENT_B",
                                    "as_of",
                                    "2026-10-22T03:04:05Z")));
            environment = new HashMap<>();
            environment.put("SPRING_CONFIG_IMPORT", "");
            environment.put("FINANCECLAW_DATABASE_URL", pg.getJdbcUrl());
            environment.put("FINANCECLAW_DATABASE_USER", pg.getUsername());
            environment.put("FINANCECLAW_DATABASE_PASSWORD", pg.getPassword());
            environment.put("FINANCECLAW_MODEL", PrivateAgentLoopback.PARENT);
            environment.put("FINANCECLAW_MODEL_BASE_URL", model.baseUrl());
            environment.put("FINANCECLAW_MODEL_API_KEY", "synthetic-packaged-key");
            environment.put("FINANCECLAW_FALLBACK_MODEL", PrivateAgentLoopback.LEAF);
            environment.put("FINANCECLAW_FALLBACK_BASE_URL", model.baseUrl());
            environment.put(
                    "FINANCECLAW_IDENTITIES_JSON",
                    json.writeValueAsString(
                            List.of(
                                    Map.of(
                                            "token",
                                            TOKEN,
                                            "tenant",
                                            "packaged",
                                            "subject",
                                            "alice",
                                            "roles",
                                            List.of("agent_manager")))));
            environment.put(
                    "FINANCECLAW_CREDENTIAL_RESOURCES_JSON",
                    json.writeValueAsString(
                            List.of(
                                    Map.of(
                                            "resourceId",
                                            "model",
                                            "reference",
                                            "@model",
                                            "tenant",
                                            "packaged",
                                            "subject",
                                            "alice"))));
            environment.put("FINANCECLAW_MARKET_FILE", marketA.toString());
            environment.put("FINANCECLAW_FEISHU_ENABLED", "false");
            environment.put("FINANCECLAW_INTEGRATIONS_ENABLED", "false");
            Path log = directory.resolve("application.log");
            var args =
                    new ArrayList<>(
                            List.of(
                                    "--server.port=0",
                                    "--spring.config.import=",
                                    "--financeclaw.worker.parallelism=1",
                                    "--financeclaw.worker.lease-ms=60000",
                                    "--financeclaw.worker.heartbeat-ms=1000",
                                    "--financeclaw.auth.token="));
            Process process =
                    start(
                            "com.financeclaw.bootstrap.FinanceClawApplication",
                            args,
                            environment,
                            log);
            try {
                awaitServer(process, log);
                String leaf = UUID.randomUUID().toString(), parent = UUID.randomUUID().toString();
                String tool = "delegate_" + leaf.replace("-", "");
                model.reset(
                        request -> {
                            if (request.model().equals(PrivateAgentLoopback.LEAF)) {
                                assertThat(request.serialized())
                                        .contains("PACKAGED_LEAF_A")
                                        .doesNotContain("PACKAGED_LEAF_B");
                                if (request.toolResult("snapshot") == null)
                                    return PrivateAgentLoopback.Reply.tool(
                                            "snapshot",
                                            "market_snapshot",
                                            Map.of("symbol", "AAPL"));
                                assertThat(request.toolResult("snapshot"))
                                        .contains("PACKAGED_TARGET_A", "101.11")
                                        .doesNotContain("PACKAGED_PARENT_B", "303.33");
                                return PrivateAgentLoopback.Reply.text(
                                        "child verified frozen market A");
                            }
                            if (request.toolResult("delegate") == null)
                                return PrivateAgentLoopback.Reply.tool(
                                        "delegate",
                                        tool,
                                        Map.of("message", "explicit packaged child input"));
                            assertThat(request.toolResult("delegate"))
                                    .contains("child verified frozen market A");
                            return PrivateAgentLoopback.Reply.text("packaged parent completed");
                        });
                call(
                        "POST",
                        "/v1/agents",
                        Map.of(
                                "agent_id",
                                leaf,
                                "display_name",
                                "leaf",
                                "system_prompt",
                                "PACKAGED_LEAF_A",
                                "composition",
                                Map.of(
                                        "model_profile",
                                        "fallback",
                                        "capabilities",
                                        List.of("function:market_snapshot"))),
                        201,
                        null);
                var leafRelease =
                        call(
                                "POST",
                                "/v1/agents/" + leaf + "/publish",
                                Map.of(
                                        "expected_draft_revision",
                                        1,
                                        "expected_deployment_revision",
                                        0),
                                200,
                                null);
                assertThat(leafRelease.path("release_hash").asText()).matches("[0-9a-f]{64}");
                // Prepare a distinct base in an independent real operator CLI, then activate it.
                var changed = new HashMap<>(environment);
                changed.put("FINANCECLAW_MARKET_FILE", marketB.toString());
                var prepared = cli(List.of("prepare"), changed, "prepare");
                assertThat(prepared.path("candidateHash").asText()).matches("[0-9a-f]{64}");
                cli(
                        List.of(
                                "activate",
                                "--hash",
                                prepared.path("candidateHash").asText(),
                                "--expected-revision",
                                prepared.path("current").path("revision").asText()),
                        changed,
                        "activate");
                call(
                        "POST",
                        "/v1/agents",
                        Map.of(
                                "agent_id",
                                parent,
                                "display_name",
                                "parent",
                                "system_prompt",
                                "PACKAGED_PARENT",
                                "composition",
                                Map.of(
                                        "model_profile",
                                        "primary",
                                        "capabilities",
                                        List.of("agent:" + leaf))),
                        201,
                        null);
                var parentRelease =
                        call(
                                "POST",
                                "/v1/agents/" + parent + "/publish",
                                Map.of(
                                        "expected_draft_revision",
                                        1,
                                        "expected_deployment_revision",
                                        0),
                                200,
                                null);
                assertThat(parentRelease.path("release_hash").asText()).matches("[0-9a-f]{64}");
                call(
                        "PUT",
                        "/v1/agents/" + leaf,
                        Map.of(
                                "expected_draft_revision",
                                1,
                                "display_name",
                                "leaf",
                                "system_prompt",
                                "PACKAGED_LEAF_B",
                                "composition",
                                Map.of(
                                        "model_profile",
                                        "fallback",
                                        "capabilities",
                                        List.of("function:market_snapshot"))),
                        200,
                        null);
                call(
                        "POST",
                        "/v1/agents/" + leaf + "/publish",
                        Map.of("expected_draft_revision", 2, "expected_deployment_revision", 1),
                        200,
                        null);
                String conversation =
                        call("POST", "/v1/conversations", Map.of("agent_id", parent), 201, null)
                                .path("conversation_id")
                                .asText();
                var accepted =
                        call(
                                "POST",
                                "/v1/conversations/" + conversation + "/turns",
                                Map.of("message", "delegate frozen market"),
                                202,
                                "packaged-turn");
                String path =
                        "/v1/conversations/"
                                + conversation
                                + "/turns/"
                                + accepted.path("turn_id").asText();
                JsonNode result = awaitTerminal(path);
                assertThat(result.path("status").asText()).isEqualTo("completed");
                assertThat(result.path("result").asText()).contains("packaged parent completed");
                assertThat(model.requests(PrivateAgentLoopback.LEAF)).hasSize(2);
                assertThat(model.requests(PrivateAgentLoopback.PARENT)).hasSize(2);
                assertThat(model.failures()).isEmpty();
                Path evidence = Path.of("target/non-rag/packaged-market-child.json");
                Files.createDirectories(evidence.getParent());
                json.writerWithDefaultPrettyPrinter()
                        .writeValue(
                                evidence.toFile(),
                                Map.of(
                                        "schema",
                                        "packaged-market-child/1",
                                        "jar_sha256",
                                        sha(Files.readAllBytes(jar)),
                                        "application_classpath",
                                        "executable JAR only",
                                        "test_classpath_in_child",
                                        false,
                                        "leaf_release_hash",
                                        leafRelease.path("release_hash").asText(),
                                        "parent_release_hash",
                                        parentRelease.path("release_hash").asText(),
                                        "leaf_models",
                                        2,
                                        "parent_models",
                                        2,
                                        "target_and_parent_data_distinguished",
                                        true,
                                        "scope",
                                        "real independent JAR, real PostgreSQL, loopback model protocol and synthetic market"));
            } finally {
                process.destroy();
                if (!process.waitFor(10, TimeUnit.SECONDS)) {
                    process.destroyForcibly();
                    assertThat(process.waitFor(10, TimeUnit.SECONDS)).isTrue();
                }
                Path logs = Path.of("target/non-rag/packaged-runtime-logs");
                Files.createDirectories(logs);
                for (String name : List.of("application.log", "prepare.log", "activate.log")) {
                    Path source = directory.resolve(name);
                    if (Files.exists(source))
                        Files.copy(source, logs.resolve(name), StandardCopyOption.REPLACE_EXISTING);
                }
            }
        }
    }

    private Process start(String main, List<String> args, Map<String, String> env, Path log)
            throws Exception {
        var command =
                new ArrayList<>(
                        List.of(
                                Path.of(System.getProperty("java.home"), "bin/java").toString(),
                                "-Dfile.encoding=UTF-8",
                                "-Dloader.main=" + main,
                                "-cp",
                                jar.toString(),
                                "org.springframework.boot.loader.launch.PropertiesLauncher"));
        command.addAll(args);
        var builder =
                new ProcessBuilder(command)
                        .directory(directory.toFile())
                        .redirectErrorStream(true)
                        .redirectOutput(log.toFile());
        builder.environment().clear();
        builder.environment().putAll(env);
        return builder.start();
    }

    private JsonNode cli(List<String> args, Map<String, String> env, String name) throws Exception {
        Path log = directory.resolve(name + ".log");
        var p = start("com.financeclawrelease.ReleaseAdministrationApplication", args, env, log);
        try {
            assertThat(p.waitFor(45, TimeUnit.SECONDS)).isTrue();
            assertThat(p.exitValue())
                    .withFailMessage("Operator CLI failed; inspect %s", log)
                    .isZero();
            return json.readTree(log.toFile());
        } finally {
            p.destroyForcibly();
            p.waitFor(5, TimeUnit.SECONDS);
        }
    }

    private void awaitServer(Process p, Path log) throws Exception {
        long end = System.nanoTime() + Duration.ofSeconds(60).toNanos();
        var pattern = Pattern.compile("Tomcat started on port (\\d+)");
        while (System.nanoTime() < end) {
            assertThat(p.isAlive()).withFailMessage("Application exited; inspect %s", log).isTrue();
            if (Files.exists(log)) {
                var matcher = pattern.matcher(Files.readString(log));
                if (matcher.find()) {
                    api = "http://127.0.0.1:" + matcher.group(1);
                    return;
                }
            }
            Thread.sleep(100);
        }
        throw new AssertionError("Packaged server startup timeout; inspect " + log);
    }

    private JsonNode call(String method, String path, Object body, int expected, String key)
            throws Exception {
        var builder =
                HttpRequest.newBuilder(URI.create(api + path))
                        .timeout(Duration.ofSeconds(15))
                        .header("Authorization", "Bearer " + TOKEN)
                        .header("Content-Type", "application/json");
        if (key != null) builder.header("Idempotency-Key", key);
        builder.method(
                method,
                body == null
                        ? HttpRequest.BodyPublishers.noBody()
                        : HttpRequest.BodyPublishers.ofString(json.writeValueAsString(body)));
        var response = http.send(builder.build(), HttpResponse.BodyHandlers.ofString());
        assertThat(response.statusCode())
                .withFailMessage("Unexpected %s %s HTTP %s", method, path, response.statusCode())
                .isEqualTo(expected);
        return json.readTree(response.body());
    }

    private JsonNode awaitTerminal(String path) throws Exception {
        long end = System.nanoTime() + Duration.ofSeconds(45).toNanos();
        while (System.nanoTime() < end) {
            var result = call("GET", path, null, 200, null);
            if (Set.of("completed", "failed", "cancelled", "blocked", "waiting")
                    .contains(result.path("status").asText())) return result;
            Thread.sleep(50);
        }
        throw new AssertionError("Packaged Turn timeout");
    }

    private static String sha(byte[] bytes) throws Exception {
        return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(bytes));
    }
}
