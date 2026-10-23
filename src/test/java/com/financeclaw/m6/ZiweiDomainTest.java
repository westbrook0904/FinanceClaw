package com.financeclaw.m6;

import static org.assertj.core.api.Assertions.*;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.financeclaw.app.release.ReleaseService;
import com.financeclaw.app.ziwei.ZiweiIdentity;
import com.financeclaw.domain.Task.Owner;
import com.financeclaw.domain.release.ZiweiPolicy;
import com.financeclaw.infrastructure.agentscope.capability.AgentScopeZiweiInputs;
import com.financeclaw.infrastructure.release.BusinessConfigurationLoader;
import com.financeclaw.infrastructure.release.EnvironmentReleaseSource;
import com.financeclaw.infrastructure.ziwei.ZiweiSchemas;
import com.financeclaw.support.PayloadDigest;
import com.financeclaw.support.PayloadJson;
import com.financeclaw.ziwei.*;
import java.time.*;
import java.util.*;
import org.junit.jupiter.api.*;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.*;
import org.springframework.mock.env.MockEnvironment;

class ZiweiDomainTest {
    static final AgentScopeZiweiInputs INPUTS = new AgentScopeZiweiInputs();
    static final String KEY = "synthetic-m6c-key-only-01234567890123456789";
    static final ZiweiPolicy POLICY =
            ZiweiPolicy.development("ZIWEI_HMAC_KEY", "test-v1", "Asia/Shanghai");
    static final ZiweiDates.QueryClock CLOCK =
            new ZiweiDates.QueryClock(Instant.parse("2025-12-31T16:00:00Z"), "Asia/Shanghai");

    static Map<String, Object> birth() {
        return new TreeMap<>(
                Map.of(
                        "calendar",
                        "solar",
                        "date",
                        Map.of("year", 1990, "month", 6, "day", 15),
                        "time",
                        Map.of("kind", "shichen", "shichen", "yin"),
                        "place",
                        Map.of("name", "上海"),
                        "sex_for_chart",
                        "male"));
    }

    static Map<String, Object> input() {
        return new TreeMap<>(Map.of("birth", birth()));
    }

    @Test
    void partialTargetAndTimeAreClarifiedTogetherAndSchemaCannotBeMutated() {
        var review =
                INPUTS.inspect(
                        "ziwei_daily_chart",
                        Map.of(
                                "birth",
                                Map.of("time", Map.of("kind", "range")),
                                "date_range",
                                Map.of("start", "2026-02-17")),
                        CLOCK);
        assertThat(review.issues())
                .extracting(ZiweiInputs.Issue::field)
                .contains("birth.time.clock", "birth.time.end", "date_range.end");
        var schema = ZiweiSchemas.schema("ziwei_natal_chart");
        assertThatThrownBy(() -> ((Map<?, ?>) schema.get("$defs")).clear())
                .isInstanceOf(UnsupportedOperationException.class);
        var original = input();
        var corrected =
                INPUTS.corrected(
                        "ziwei_natal_chart",
                        original,
                        Map.of("birth", Map.of("time", Map.of("kind", "clock", "clock", "03:30"))));
        assertThat(INPUTS.inspect("ziwei_natal_chart", corrected, CLOCK).complete()).isTrue();
    }

    @Test
    void frozenDomainFixtureAndFiveToolFieldSetsAreVerifiable() throws Exception {
        var json = new ObjectMapper();
        byte[] fixture = getClass().getResourceAsStream("/ziwei/domain/domain.json").readAllBytes();
        var manifest = json.readTree(getClass().getResourceAsStream("/ziwei/domain/manifest.json"));
        assertThat(
                        PayloadDigest.sha256(
                                new String(fixture, java.nio.charset.StandardCharsets.UTF_8)))
                .isEqualTo(manifest.get("fixture_sha256").asText());
        var schemas =
                json.readTree(getClass().getResourceAsStream("/ziwei/reference/schemas.json"));
        for (var level : ZiweiDates.Level.values()) {
            var old = new TreeSet<String>();
            schemas.get(level.toolName()).get("properties").fieldNames().forEachRemaining(old::add);
            assertThat(
                            ((Map<?, ?>) ZiweiSchemas.schema(level.toolName()).get("properties"))
                                    .keySet())
                    .isEqualTo(old);
        }
    }

    @Test
    void aggregatesMissingBirthAndTargetWithoutInventingDefaults() {
        var review =
                INPUTS.inspect(
                        "ziwei_monthly_chart",
                        Map.of("birth", Map.of("date", Map.of("year", 1990))),
                        CLOCK);
        assertThat(review.complete()).isFalse();
        assertThat(review.issues())
                .extracting(ZiweiInputs.Issue::field)
                .contains(
                        "birth.calendar",
                        "birth.place",
                        "birth.sex_for_chart",
                        "birth.time",
                        "birth.date.month",
                        "birth.date.day",
                        "target");
        assertThat(review.question()).doesNotContain("1990");
    }

    @ParameterizedTest
    @ValueSource(
            strings = {
                "tenant_id",
                "subject_id",
                "convention",
                "key_version",
                "request_clock",
                "timezone",
                "target",
                "level"
            })
    void rejectsModelIdentityRulesAndGenericTargets(String field) {
        var input = input();
        input.put(field, "untrusted");
        assertThat(INPUTS.inspect("ziwei_natal_chart", input, CLOCK).issues())
                .contains(new ZiweiInputs.Issue("arguments", "ZIWEI_TOOL_INPUT_INVALID"));
    }

    @ParameterizedTest
    @MethodSource("badBirths")
    void semanticBirthErrorsAreSafeClarifications(Map<String, Object> change, String code) {
        var birth = birth();
        birth.putAll(change);
        var result = INPUTS.inspect("ziwei_natal_chart", Map.of("birth", birth), CLOCK);
        assertThat(result.complete()).isFalse();
        assertThat(result.issues()).extracting(ZiweiInputs.Issue::code).contains(code);
        assertThat(result.question()).doesNotContain("private-city");
    }

    static java.util.stream.Stream<Arguments> badBirths() {
        return java.util.stream.Stream.of(
                Arguments.of(Map.of("calendar", "lunar"), "ZIWEI_INPUT_INCOMPLETE"),
                Arguments.of(
                        Map.of("date", Map.of("year", 1990, "month", 2, "day", 30)),
                        "ZIWEI_DATE_UNSUPPORTED"),
                Arguments.of(
                        Map.of("date", Map.of("year", 1990.0, "month", 6, "day", 15)),
                        "ZIWEI_TOOL_INPUT_INVALID"),
                Arguments.of(
                        Map.of("time_basis", "apparent_solar"), "ZIWEI_CONVENTION_UNSUPPORTED"),
                Arguments.of(
                        Map.of("place", Map.of("name", "上海", "timezone", "Asia/Taipei")),
                        "ZIWEI_PLACE_AMBIGUOUS"),
                Arguments.of(
                        Map.of("place", Map.of("name", "上海", "country_code", "US")),
                        "ZIWEI_PLACE_AMBIGUOUS"),
                Arguments.of(
                        Map.of("place", Map.of("name", "private-city")), "ZIWEI_PLACE_AMBIGUOUS"),
                Arguments.of(
                        Map.of("time", Map.of("kind", "shichen", "shichen", "zi")),
                        "ZIWEI_TIME_AMBIGUOUS"),
                Arguments.of(
                        Map.of("time", Map.of("kind", "range", "clock", "02:59", "end", "03:00")),
                        "ZIWEI_TIME_AMBIGUOUS"));
    }

    @Test
    void preciseClockAndRangePreservePrecisionAndDstFold() {
        var birth = birth();
        birth.put("place", Map.of("name", "New York", "timezone", "America/New_York"));
        birth.put("date", Map.of("year", 2024, "month", 11, "day", 3));
        birth.put("time", Map.of("kind", "clock", "clock", "01:30"));
        assertThat(INPUTS.inspect("ziwei_natal_chart", Map.of("birth", birth), CLOCK).complete())
                .isFalse();
        birth.put("time", Map.of("kind", "clock", "clock", "01:30", "fold", 1));
        assertThat(
                        INPUTS.inspect("ziwei_natal_chart", Map.of("birth", birth), CLOCK)
                                .value()
                                .time()
                                .utcInstant())
                .isEqualTo(Instant.parse("2024-11-03T06:30:00Z"));
        birth.put("time", Map.of("kind", "range", "clock", "03:01", "end", "04:30"));
        assertThat(
                        INPUTS.inspect("ziwei_natal_chart", Map.of("birth", birth), CLOCK)
                                .value()
                                .time()
                                .utcInstant())
                .isNull();
    }

    @Test
    void clarificationPreservesKnownFieldsAndReplacesConflictingSelectors() {
        var input = input();
        input.put("year_offset", 0);
        var corrected =
                INPUTS.corrected(
                        "ziwei_yearly_chart",
                        input,
                        Map.of("year", 2027, "birth", Map.of("sex_for_chart", "female")));
        assertThat(corrected).doesNotContainKey("year_offset").containsEntry("year", 2027);
        assertThat((Map<?, ?>) corrected.get("birth")).hasSize(birth().size());
        assertThat(INPUTS.inspect("ziwei_yearly_chart", corrected, CLOCK).complete()).isTrue();
        assertThatThrownBy(
                        () ->
                                INPUTS.corrected(
                                        "ziwei_yearly_chart", input, Map.of("mode", "chart_only")))
                .isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void hmacBindsOwnerRulesKeyAndTimePrecisionWithoutRawIdentityInRelease() {
        var normalized = INPUTS.inspect("ziwei_natal_chart", input(), CLOCK).value();
        String first =
                ZiweiIdentity.fingerprint(
                        normalized, new Owner("one", "user"), POLICY, ZiweiPolicy.key(KEY));
        assertThat(first)
                .hasSize(64)
                .isNotEqualTo(
                        ZiweiIdentity.fingerprint(
                                normalized, new Owner("two", "user"), POLICY, ZiweiPolicy.key(KEY)))
                .isNotEqualTo(
                        ZiweiIdentity.fingerprint(
                                normalized,
                                new Owner("one", "other"),
                                POLICY,
                                ZiweiPolicy.key(KEY)))
                .isNotEqualTo(
                        ZiweiIdentity.fingerprint(
                                normalized,
                                new Owner("one", "user"),
                                POLICY,
                                ZiweiPolicy.key(KEY + "rotate")));
        var env =
                new MockEnvironment()
                        .withProperty("financeclaw.business.ziwei.enabled", "true")
                        .withProperty("financeclaw.business.ziwei.development", "true")
                        .withProperty("financeclaw.business.ziwei.key-ref", "ZIWEI_HMAC_KEY")
                        .withProperty("financeclaw.business.ziwei.key-version", "test-v1")
                        .withProperty("financeclaw.business.ziwei.query-timezone", "Asia/Shanghai")
                        .withProperty("ZIWEI_HMAC_KEY", KEY);
        var catalog = new ReleaseService(new EnvironmentReleaseSource(env));
        String hash = catalog.currentHash();
        assertThat(PayloadJson.canonical(catalog.require(hash)))
                .doesNotContain(KEY)
                .contains(POLICY.convention(), "2026b", POLICY.schemaHash());
        env.setProperty("financeclaw.business.ziwei.key-version", "test-v2");
        assertThat(new ReleaseService(new EnvironmentReleaseSource(env)).currentHash())
                .isNotEqualTo(hash);
        assertThatThrownBy(() -> ZiweiPolicy.key("short")).hasMessage("ziwei_key_unavailable");
        assertThat(
                        BusinessConfigurationLoader.load(
                                        new MockEnvironment()
                                                .withProperty(
                                                        "financeclaw.business.ziwei.key-ref",
                                                        "${MISSING}"))
                                .ziwei())
                .isNull();
    }

    @TestFactory
    Collection<DynamicTest> factsAndIntervalsMatchFrozenLegacyDomain() throws Exception {
        var mapper = new ObjectMapper();
        var cases = mapper.readTree(getClass().getResourceAsStream("/ziwei/domain/domain.json"));
        var tests = new ArrayList<DynamicTest>();
        for (var c : cases) {
            var level = ZiweiDates.Level.valueOf(c.get("level").asText().toUpperCase(Locale.ROOT));
            for (String focus : List.of("overall", "career", "relationship", "wealth")) {
                tests.add(
                        DynamicTest.dynamicTest(
                                level + " " + c.get("start") + " " + focus,
                                () -> {
                                    var input = input();
                                    input.put("focus", focus);
                                    if (!c.get("start").isNull())
                                        input.put(
                                                "date_range",
                                                Map.of(
                                                        "start",
                                                        c.get("start").asText(),
                                                        "end",
                                                        c.get("end").asText()));
                                    var review = INPUTS.inspect(level.toolName(), input, CLOCK);
                                    assertThat(review.complete()).isTrue();
                                    if (c.has("error")) {
                                        assertThatThrownBy(
                                                        () ->
                                                                ZiweiCalculation.calculate(
                                                                        review.value(),
                                                                        level,
                                                                        "synthetic",
                                                                        POLICY))
                                                .isInstanceOf(ZiweiDates.InputError.class)
                                                .hasMessageStartingWith(c.get("error").asText());
                                    } else {
                                        var result =
                                                ZiweiCalculation.calculate(
                                                        review.value(), level, "synthetic", POLICY);
                                        assertThat(
                                                        mapper
                                                                .<com.fasterxml.jackson.databind
                                                                                .JsonNode>
                                                                        valueToTree(
                                                                                result.artifact()
                                                                                        .get(
                                                                                                "facts")))
                                                .isEqualTo(c.get("facts"));
                                        assertThat(
                                                        mapper
                                                                .<com.fasterxml.jackson.databind
                                                                                .JsonNode>
                                                                        valueToTree(
                                                                                result.artifact()
                                                                                        .get(
                                                                                                "segments")))
                                                .isEqualTo(c.get("segments"));
                                        var projection = mapper.valueToTree(result.projection());
                                        var ids = new ArrayList<String>();
                                        projection
                                                .get("facts")
                                                .forEach(f -> ids.add(f.get("fact_id").asText()));
                                        assertThat(
                                                        mapper
                                                                .<com.fasterxml.jackson.databind
                                                                                .JsonNode>
                                                                        valueToTree(ids))
                                                .isEqualTo(c.get("projections").get(focus));
                                        assertThat(ZiweiCalculation.bytes(result.projection()))
                                                .isLessThanOrEqualTo(14000);
                                    }
                                }));
            }
        }
        return tests;
    }

    @Test
    void budgetsFailWithoutTruncatingAndFocusDoesNotChangeFullChart() {
        var input = input();
        var full = INPUTS.inspect("ziwei_natal_chart", input, CLOCK).value();
        input.put("focus", "career");
        var focused = INPUTS.inspect("ziwei_natal_chart", input, CLOCK).value();
        var a = ZiweiCalculation.calculate(full, ZiweiDates.Level.NATAL, "same", POLICY);
        var b = ZiweiCalculation.calculate(focused, ZiweiDates.Level.NATAL, "same", POLICY);
        assertThat(a.artifact()).isEqualTo(b.artifact());
        var small =
                new ZiweiPolicy(
                        POLICY.convention(),
                        POLICY.engine(),
                        POLICY.calendar(),
                        POLICY.tzdb(),
                        POLICY.schemaHash(),
                        POLICY.keyVersion(),
                        POLICY.keyRef(),
                        POLICY.queryTimezone(),
                        32,
                        1024,
                        2097152);
        assertThatThrownBy(
                        () ->
                                ZiweiCalculation.calculate(
                                        full, ZiweiDates.Level.NATAL, "same", small))
                .hasMessageStartingWith("ZIWEI_CONTEXT_BUDGET_EXCEEDED");
        var smallArtifact =
                new ZiweiPolicy(
                        POLICY.convention(),
                        POLICY.engine(),
                        POLICY.calendar(),
                        POLICY.tzdb(),
                        POLICY.schemaHash(),
                        POLICY.keyVersion(),
                        POLICY.keyRef(),
                        POLICY.queryTimezone(),
                        32,
                        14000,
                        1024);
        assertThatThrownBy(
                        () ->
                                ZiweiCalculation.calculate(
                                        full, ZiweiDates.Level.NATAL, "same", smallArtifact))
                .hasMessageStartingWith("ZIWEI_ARTIFACT_BUDGET_EXCEEDED");
        assertThatThrownBy(() -> a.artifact().put("forged", true))
                .isInstanceOf(UnsupportedOperationException.class);
        var single =
                new ZiweiPolicy(
                        POLICY.convention(),
                        POLICY.engine(),
                        POLICY.calendar(),
                        POLICY.tzdb(),
                        POLICY.schemaHash(),
                        POLICY.keyVersion(),
                        POLICY.keyRef(),
                        POLICY.queryTimezone(),
                        1,
                        14000,
                        2097152);
        input.put("year", 2026);
        var year = INPUTS.inspect("ziwei_yearly_chart", input, CLOCK).value();
        assertThatThrownBy(
                        () ->
                                ZiweiCalculation.calculate(
                                        year, ZiweiDates.Level.YEARLY, "same", single))
                .hasMessageStartingWith("ZIWEI_RANGE_LIMIT");
    }
}
