package com.financeclaw.m6;

import static org.assertj.core.api.Assertions.*;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.financeclaw.ziwei.ZiweiCalendar;
import com.financeclaw.ziwei.ZiweiCalendar.Calendar;
import com.financeclaw.ziwei.ZiweiCalendar.LunarDate;
import com.financeclaw.ziwei.ZiweiDates;
import com.nlf.calendar.Solar;
import java.nio.file.*;
import java.security.MessageDigest;
import java.time.*;
import java.time.temporal.ChronoUnit;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.stream.*;
import org.junit.jupiter.api.*;

/** 使用锁定原生库验证日历适配边界，不维持其他应用的旧历法数据。 */
class ZiweiCalendarTest {
    private static final ObjectMapper JSON = new ObjectMapper();
    private static final LocalDate FIRST = LocalDate.of(1901, 1, 1);
    private static final LocalDate END = LocalDate.of(2100, 1, 1);
    private static final AtomicInteger MATCHED_DAYS = new AtomicInteger();
    private static final AtomicInteger REVERSED_DAYS = new AtomicInteger();

    private static LunarDate nativeDate(LocalDate solar) {
        var lunar =
                Solar.fromYmd(solar.getYear(), solar.getMonthValue(), solar.getDayOfMonth())
                        .getLunar();
        return new LunarDate(
                lunar.getYear(), Math.abs(lunar.getMonth()), lunar.getDay(), lunar.getMonth() < 0);
    }

    @TestFactory
    Stream<DynamicTest> allSupportedDaysUseThePinnedLibraryAndRoundTrip() {
        return IntStream.rangeClosed(1901, 2099)
                .mapToObj(
                        year ->
                                DynamicTest.dynamicTest(
                                        "calendar-" + year,
                                        () -> {
                                            for (LocalDate solar = LocalDate.of(year, 1, 1);
                                                    solar.getYear() == year;
                                                    solar = solar.plusDays(1)) {
                                                var lunar = nativeDate(solar);
                                                assertThat(ZiweiCalendar.lunarDate(solar))
                                                        .as("solar %s", solar)
                                                        .isEqualTo(lunar);
                                                if (lunar.year() >= 1901) {
                                                    assertThat(
                                                                    ZiweiCalendar.solarDate(
                                                                            Calendar.LUNAR,
                                                                            lunar.year(),
                                                                            lunar.month(),
                                                                            lunar.day(),
                                                                            lunar.leap()))
                                                            .as("lunar corresponding to %s", solar)
                                                            .isEqualTo(solar);
                                                    REVERSED_DAYS.incrementAndGet();
                                                }
                                                MATCHED_DAYS.incrementAndGet();
                                            }
                                        }));
    }

    @Test
    void explicitLunarMonthBoundariesFollowThePinnedNativeCalendar() {
        var samples =
                Map.of(
                        "1925-05-21", new LunarDate(1925, 4, 29, false),
                        "1925-05-22", new LunarDate(1925, 4, 1, true),
                        "1925-06-20", new LunarDate(1925, 4, 30, true),
                        "1925-06-21", new LunarDate(1925, 5, 1, false),
                        "2057-09-28", new LunarDate(2057, 8, 30, false),
                        "2057-09-29", new LunarDate(2057, 9, 1, false),
                        "2057-10-27", new LunarDate(2057, 9, 29, false),
                        "2057-10-28", new LunarDate(2057, 10, 1, false));
        samples.forEach(
                (day, expected) -> {
                    assertThat(ZiweiCalendar.lunarDate(LocalDate.parse(day))).isEqualTo(expected);
                    assertThat(
                                    ZiweiCalendar.solarDate(
                                            Calendar.LUNAR,
                                            expected.year(),
                                            expected.month(),
                                            expected.day(),
                                            expected.leap()))
                            .isEqualTo(LocalDate.parse(day));
                });
        assertThatThrownBy(() -> ZiweiCalendar.solarDate(Calendar.LUNAR, 1925, 4, 30, false))
                .hasMessage("ZIWEI_DATE_UNSUPPORTED: birth.date");
        assertThatThrownBy(() -> ZiweiCalendar.solarDate(Calendar.LUNAR, 2057, 9, 30, false))
                .hasMessage("ZIWEI_DATE_UNSUPPORTED: birth.date");
    }

    @Test
    void leapFlagsMissingValuesInvalidDaysAndSupportedRangeAreExplicit() {
        assertThat(ZiweiCalendar.solarDate(Calendar.LUNAR, 2023, 2, 1, true))
                .isEqualTo(LocalDate.of(2023, 3, 22));
        assertThat(ZiweiCalendar.solarDate(Calendar.LUNAR, 2023, 2, 29, true))
                .isEqualTo(LocalDate.of(2023, 4, 19));
        assertThatThrownBy(() -> ZiweiCalendar.solarDate(Calendar.LUNAR, 2023, 2, 30, true))
                .hasMessage("ZIWEI_DATE_UNSUPPORTED: birth.date");
        assertThatThrownBy(() -> ZiweiCalendar.solarDate(Calendar.LUNAR, 2024, 2, 1, true))
                .hasMessage("ZIWEI_DATE_UNSUPPORTED: birth.date");
        assertThatThrownBy(() -> ZiweiCalendar.solarDate(Calendar.SOLAR, 2023, 2, 29, false))
                .hasMessage("ZIWEI_DATE_UNSUPPORTED: birth.date");
        assertThatThrownBy(() -> ZiweiCalendar.solarDate(Calendar.SOLAR, 2024, 2, 29, true))
                .hasMessage("ZIWEI_DATE_UNSUPPORTED: birth.date");
        assertThatThrownBy(() -> ZiweiCalendar.solarDate(null, 2000, 7, 17, false))
                .hasMessage("ZIWEI_INPUT_INCOMPLETE: birth.calendar");
        assertThatThrownBy(() -> ZiweiCalendar.solarDate(Calendar.LUNAR, 2000, 7, 17, null))
                .hasMessage("ZIWEI_INPUT_INCOMPLETE: birth.is_leap_month");
        assertThat(ZiweiCalendar.lunarDate(FIRST)).isEqualTo(new LunarDate(1900, 11, 11, false));
        assertThat(ZiweiCalendar.lunarDate(END.minusDays(1)))
                .isEqualTo(new LunarDate(2099, 11, 20, false));
        for (var outside : List.of(FIRST.minusDays(1), END)) {
            assertThatThrownBy(() -> ZiweiCalendar.lunarDate(outside))
                    .hasMessage("ZIWEI_DATE_UNSUPPORTED: birth.date");
        }
    }

    @Test
    void exactPublishedArtifactAndLicenseArePinned() throws Exception {
        var lock = JSON.readTree(Path.of("ziwei-dependencies.lock.json").toFile()).get("calendar");
        assertThat(lock.get("coordinate").asText()).isEqualTo(ZiweiCalendar.ENGINE);
        assertThat(lock.get("calendar_rules").asText()).isEqualTo(ZiweiCalendar.RULES);
        Path jar = Path.of(Solar.class.getProtectionDomain().getCodeSource().getLocation().toURI());
        assertThat(sha(Files.readAllBytes(jar)))
                .isEqualTo(lock.get("artifact_sha256").get("lunar-1.7.7.jar").asText());
        try (var license = getClass().getResourceAsStream("/META-INF/licenses/lunar-1.7.7.txt")) {
            assertThat(license).isNotNull();
            assertThat(sha(license.readAllBytes())).isEqualTo(lock.get("license_sha256").asText());
        }
    }

    @Test
    void simultaneousDifferentYearsHaveDeterministicIndependentResults() throws Exception {
        var samples =
                IntStream.rangeClosed(1901, 2099)
                        .mapToObj(year -> LocalDate.of(year, 7, 1))
                        .toList();
        try (var executor = Executors.newFixedThreadPool(4)) {
            var jobs = new ArrayList<Future<LunarDate>>();
            for (var sample : samples)
                jobs.add(executor.submit(() -> ZiweiCalendar.lunarDate(sample)));
            for (int index = 0; index < jobs.size(); index++) {
                assertThat(jobs.get(index).get(10, TimeUnit.SECONDS))
                        .isEqualTo(nativeDate(samples.get(index)));
            }
        }
    }

    @AfterAll
    static void writeEvidence() throws Exception {
        Path output = Path.of("target/m6/calendar-results.json");
        Files.createDirectories(output.getParent());
        JSON.writerWithDefaultPrettyPrinter()
                .writeValue(
                        output.toFile(),
                        Map.of(
                                "scope",
                                "Pinned native lunar adapter and supported-range roundtrips; no external application calendar oracle",
                                "calendar_engine",
                                ZiweiCalendar.ENGINE,
                                "calendar_rules",
                                ZiweiCalendar.RULES,
                                "matched_solar_days",
                                MATCHED_DAYS.get(),
                                "expected_solar_days",
                                ChronoUnit.DAYS.between(FIRST, END),
                                "reversed_lunar_days",
                                REVERSED_DAYS.get(),
                                "java_tzdb",
                                ZiweiDates.tzdbVersion("Asia/Shanghai"),
                                "java_chart_engine_verified",
                                false));
    }

    private static String sha(byte[] bytes) throws Exception {
        return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(bytes));
    }
}
