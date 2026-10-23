package com.financeclaw.m6;

import static org.assertj.core.api.Assertions.*;

import com.fasterxml.jackson.databind.*;
import java.nio.file.*;
import java.security.MessageDigest;
import java.util.*;
import java.util.concurrent.TimeUnit;
import java.util.zip.*;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

class ZiweiPackagedIT {
    @TempDir Path directory;

    @Test
    void packagedEngineRunsInSeparateJvmsWithoutFixtureClasspathOrInterpreterEnvironment()
            throws Exception {
        var json = new ObjectMapper();
        Path application = Path.of("target/financeclaw-java-0.1.0-SNAPSHOT.jar").toAbsolutePath();
        var lock = json.readTree(Path.of("ziwei-dependencies.lock.json").toFile());
        try (var zip = new ZipFile(application.toFile())) {
            assertThat(
                            zip.stream()
                                    .map(ZipEntry::getName)
                                    .filter(
                                            n ->
                                                    n.endsWith(".py")
                                                            || n.contains("ziwei/reference/")
                                                            || n.contains("ziwei/calendar/")
                                                            || n.contains("ziwei/charts/")
                                                            || n.contains("ziwei/domain/")
                                                            || n.contains("ZiweiRuntimeProbe")
                                                            || n.contains("ZiweiPackagedProbe"))
                                    .toList())
                    .isEmpty();
            for (var notice :
                    List.of(
                            lock.get("calendar"),
                            lock.get("algorithm_reference"),
                            lock.get("algorithm_reference").get("upstream_notice"))) {
                String path =
                        notice.get("license_path").asText().replace("src/main/resources/", "");
                try (var stream = zip.getInputStream(zip.getEntry(path))) {
                    assertThat(sha(stream.readAllBytes()))
                            .isEqualTo(notice.get("license_sha256").asText());
                }
            }
        }
        String resource = "com/financeclaw/m6/ZiweiPackagedProbe.class";
        Path launcher = directory.resolve("probe.jar");
        try (var output = new ZipOutputStream(Files.newOutputStream(launcher));
                var probe = getClass().getResourceAsStream("/" + resource)) {
            output.putNextEntry(new ZipEntry(resource));
            output.write(Objects.requireNonNull(probe).readAllBytes());
            output.closeEntry();
        }
        var expected =
                json.readTree(Path.of("src/test/resources/ziwei/reference/charts.json").toFile());
        var input = json.createArrayNode();
        for (var row : expected) {
            var minimal = input.addObject();
            minimal.set("id", row.get("id"));
            minimal.set("input", row.get("input"));
        }
        json.writeValue(directory.resolve("input.json").toFile(), input);
        var zones = List.of("UTC", "Pacific/Auckland");
        for (int i = 0; i < zones.size(); i++) {
            Path log = directory.resolve("process-" + i + ".log");
            var builder =
                    new ProcessBuilder(
                                    Path.of(System.getProperty("java.home"), "bin/java").toString(),
                                    "-Dfile.encoding=UTF-8",
                                    "-Duser.timezone=" + zones.get(i),
                                    "-Dloader.main=" + ZiweiPackagedProbe.class.getName(),
                                    "-Dloader.path=" + launcher,
                                    "-cp",
                                    application.toString(),
                                    "org.springframework.boot.loader.launch.PropertiesLauncher",
                                    "input.json",
                                    "output.json")
                            .directory(directory.toFile())
                            .redirectErrorStream(true)
                            .redirectOutput(log.toFile());
            builder.environment().clear();
            builder.environment().put("PATH", "");
            Process process = builder.start();
            try {
                assertThat(process.waitFor(30, TimeUnit.SECONDS))
                        .as("packaged process completes")
                        .isTrue();
                assertThat(process.exitValue())
                        .withFailMessage("Packaged probe failed: %s", Files.readString(log))
                        .isZero();
                var actual = json.readTree(directory.resolve("output.json").toFile());
                assertThat(ZiweiChartEngineTest.differences(expected, actual, "charts")).isEmpty();
            } finally {
                process.destroyForcibly();
            }
        }
        Path evidence = Path.of("target/m6/chart-packaging.json");
        Files.createDirectories(evidence.getParent());
        json.writerWithDefaultPrettyPrinter()
                .writeValue(
                        evidence.toFile(),
                        Map.of(
                                "application_jar_sha256",
                                sha(Files.readAllBytes(application)),
                                "matched_charts_per_process",
                                expected.size(),
                                "process_timezones",
                                zones,
                                "environment_cleared",
                                true,
                                "path_empty",
                                true,
                                "fixtures_packaged",
                                false,
                                "licenses_verified",
                                3,
                                "scope",
                                "Application jar and test-only launcher in a temporary working directory; no Python process or fixture classpath"));
    }

    private static String sha(byte[] bytes) throws Exception {
        return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(bytes));
    }
}
