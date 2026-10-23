package com.financeclaw.m6;

import static org.assertj.core.api.Assertions.*;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.*;
import com.financeclaw.ziwei.ZiweiDates;
import com.financeclaw.ziwei.ZiweiDates.QueryClock;
import java.nio.file.*;
import java.security.MessageDigest;
import java.time.*;
import java.util.*;
import java.util.concurrent.Executors;
import java.util.stream.Stream;
import org.junit.jupiter.api.*;

class ZiweiDatesTest {
    private static final ObjectMapper JSON = new ObjectMapper();
    private static final Path REFERENCE = Path.of("src/test/resources/ziwei/reference");

    private static JsonNode read(String name) throws Exception {
        return JSON.readTree(REFERENCE.resolve(name).toFile());
    }

    private static Map<String, Object> map(JsonNode node) {
        return JSON.convertValue(node, new TypeReference<>() {});
    }

    @TestFactory
    Stream<DynamicTest> matchesFrozenPythonTargets() throws Exception {
        var cases = read("dates.json").get("targets");
        return java.util.stream.StreamSupport.stream(cases.spliterator(), false)
                .map(
                        test ->
                                DynamicTest.dynamicTest(
                                        test.get("id").asText(),
                                        () -> {
                                            QueryClock clock =
                                                    test.get("request_clock").isNull()
                                                            ? null
                                                            : new QueryClock(
                                                                    OffsetDateTime.parse(
                                                                                    test.get(
                                                                                                    "request_clock")
                                                                                            .asText())
                                                                            .toInstant(),
                                                                    test.get("timezone").asText());
                                            var arguments = map(test.get("arguments"));
                                            String tool = test.get("tool").asText();
                                            if (test.has("error")) {
                                                assertThatThrownBy(
                                                                () ->
                                                                        ZiweiDates.resolve(
                                                                                tool, arguments,
                                                                                clock))
                                                        .isInstanceOf(ZiweiDates.InputError.class);
                                            } else {
                                                var actual =
                                                        ZiweiDates.resolve(tool, arguments, clock);
                                                if (test.get("expected").isNull())
                                                    assertThat(actual).isNull();
                                                else {
                                                    assertThat(actual.start().toString())
                                                            .isEqualTo(
                                                                    test.get("expected")
                                                                            .get("start")
                                                                            .asText());
                                                    assertThat(actual.end().toString())
                                                            .isEqualTo(
                                                                    test.get("expected")
                                                                            .get("end")
                                                                            .asText());
                                                    assertThat(actual.clock()).isEqualTo(clock);
                                                }
                                            }
                                            assertThat(JSON.<JsonNode>valueToTree(arguments))
                                                    .isEqualTo(test.get("arguments"));
                                        }));
    }

    @TestFactory
    Stream<DynamicTest> matchesFrozenPythonCivilTime() throws Exception {
        var cases = read("dates.json").get("civil_times");
        return java.util.stream.StreamSupport.stream(cases.spliterator(), false)
                .map(
                        test ->
                                DynamicTest.dynamicTest(
                                        test.get("id").asText(),
                                        () -> {
                                            var date =
                                                    LocalDate.parse(
                                                            test.get("solar_date").asText());
                                            String zone = test.get("timezone").asText();
                                            var input = map(test.get("input"));
                                            if (test.has("error")) {
                                                assertThatThrownBy(
                                                                () ->
                                                                        ZiweiDates.civilTime(
                                                                                date, zone, input))
                                                        .isInstanceOf(ZiweiDates.InputError.class)
                                                        .extracting(
                                                                error ->
                                                                        ((ZiweiDates.InputError)
                                                                                        error)
                                                                                .code())
                                                        .isEqualTo(test.get("error").asText());
                                            } else {
                                                var actual =
                                                        ZiweiDates.civilTime(date, zone, input);
                                                var expected = test.get("expected");
                                                assertThat(actual.shichen())
                                                        .isEqualTo(
                                                                expected.get("shichen").asText());
                                                assertThat(actual.precision())
                                                        .isEqualTo(
                                                                expected.get("precision").asText());
                                                assertThat(actual.utcInstant())
                                                        .isEqualTo(
                                                                expected.get("utc_instant").isNull()
                                                                        ? null
                                                                        : OffsetDateTime.parse(
                                                                                        expected.get(
                                                                                                        "utc_instant")
                                                                                                .asText())
                                                                                .toInstant());
                                            }
                                            assertThat(JSON.<JsonNode>valueToTree(input))
                                                    .isEqualTo(test.get("input"));
                                        }));
    }

    @Test
    void baselineContainsActualChartsAndFiveSchemasWithVerifiedDigests() throws Exception {
        var manifest = read("manifest.json");
        for (var fields = manifest.get("fixture_sha256").fields(); fields.hasNext(); ) {
            var field = fields.next();
            assertThat(
                            HexFormat.of()
                                    .formatHex(
                                            MessageDigest.getInstance("SHA-256")
                                                    .digest(
                                                            Files.readAllBytes(
                                                                    REFERENCE.resolve(
                                                                            field.getKey())))))
                    .isEqualTo(field.getValue().asText());
        }
        var schemas = read("schemas.json");
        assertThat(schemas.size()).isEqualTo(5);
        for (var level : ZiweiDates.Level.values())
            assertThat(schemas.has(level.toolName())).isTrue();
        var charts = read("charts.json");
        assertThat(charts.size()).isEqualTo(9);
        for (var chart : charts) {
            assertThat(chart.get("natal").get("palaces").size()).isEqualTo(12);
            for (String level : List.of("decadal", "yearly", "monthly", "daily"))
                assertThat(chart.get("horoscope").hasNonNull(level)).isTrue();
        }
        assertThat(charts.get(0).get("natal")).isEqualTo(charts.get(2).get("natal"));
        // Record the actual JDK timezone database; parity samples do not certify every historical
        // zone.
        Path output = Path.of("target/m6");
        Files.createDirectories(output);
        JSON.writerWithDefaultPrettyPrinter()
                .writeValue(
                        output.resolve("dates-environment.json").toFile(),
                        Map.of(
                                "java",
                                System.getProperty("java.runtime.version"),
                                "java_tzdb",
                                ZiweiDates.tzdbVersion("Asia/Shanghai"),
                                "python_tzdata",
                                "2026.3",
                                "target_cases",
                                manifest.get("cases").get("targets").asInt(),
                                "civil_time_cases",
                                manifest.get("cases").get("civil_times").asInt(),
                                "chart_cases",
                                9,
                                "java_chart_engine_verified",
                                false));
    }

    @Test
    void concurrentRequestsKeepTheirOwnPinnedClockAndTimezone() throws Exception {
        try (var executor = Executors.newFixedThreadPool(4)) {
            var jobs = new ArrayList<java.util.concurrent.Future<ZiweiDates.Target>>();
            for (int i = 0; i < 40; i++) {
                String zone = i % 2 == 0 ? "Asia/Shanghai" : "America/New_York";
                var clock = new QueryClock(Instant.parse("2025-12-31T16:30:00Z"), zone);
                jobs.add(
                        executor.submit(
                                () ->
                                        ZiweiDates.resolve(
                                                "ziwei_yearly_chart",
                                                Map.of("year_offset", 0),
                                                clock)));
            }
            for (int i = 0; i < jobs.size(); i++)
                assertThat(jobs.get(i).get().start())
                        .isEqualTo(LocalDate.of(i % 2 == 0 ? 2026 : 2025, 1, 1));
        }
    }

    @Test
    void rejectsCoercionExtraFieldsAndUntrustedTimezonesWithoutLeakingInput() {
        var clock = new QueryClock(Instant.parse("2026-01-01T00:00:00Z"), "Asia/Shanghai");
        for (Object invalid : List.of(true, "1", 1.5, Long.MAX_VALUE))
            assertThatThrownBy(
                            () ->
                                    ZiweiDates.resolve(
                                            "ziwei_daily_chart",
                                            Map.of("day_offset", invalid),
                                            clock))
                    .isInstanceOf(ZiweiDates.InputError.class);
        assertThatThrownBy(() -> new QueryClock(Instant.EPOCH, "+08:00"))
                .isInstanceOf(ZiweiDates.InputError.class);
        assertThatThrownBy(
                        () ->
                                ZiweiDates.resolve(
                                        "ziwei_daily_chart",
                                        Map.of("on_date", "private-invalid-date"),
                                        clock))
                .hasMessage("ZIWEI_TOOL_INPUT_INVALID: on_date");
        assertThatThrownBy(
                        () ->
                                ZiweiDates.resolve(
                                        "ziwei_daily_chart",
                                        Map.of(
                                                "date_range",
                                                Map.of(
                                                        "start",
                                                        "2026-01-01",
                                                        "end",
                                                        "2026-01-02",
                                                        "timezone",
                                                        "UTC")),
                                        clock))
                .isInstanceOf(ZiweiDates.InputError.class);
    }
}
