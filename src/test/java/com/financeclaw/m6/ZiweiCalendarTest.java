package com.financeclaw.m6;

import static org.assertj.core.api.Assertions.*;

import com.fasterxml.jackson.databind.*;
import com.financeclaw.ziwei.ZiweiCalendar;
import com.financeclaw.ziwei.ZiweiCalendar.Calendar;
import com.financeclaw.ziwei.ZiweiCalendar.LunarDate;
import com.financeclaw.ziwei.ZiweiDates;
import com.nlf.calendar.Solar;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.security.MessageDigest;
import java.time.*;
import java.time.temporal.ChronoUnit;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.stream.*;
import java.util.zip.GZIPInputStream;
import org.junit.jupiter.api.*;

class ZiweiCalendarTest {
  private static final ObjectMapper JSON = new ObjectMapper();
  private static final Path REFERENCE = Path.of("src/test/resources/ziwei/calendar");
  private static final AtomicInteger MATCHED_DAYS = new AtomicInteger();
  private static final AtomicInteger REVERSED_DAYS = new AtomicInteger();
  private static final AtomicInteger MATCHED_ZONES = new AtomicInteger();

  private record Row(LocalDate solar, LunarDate lunar) {}

  private static JsonNode manifest() throws IOException {
    return JSON.readTree(REFERENCE.resolve("calendar-manifest.json").toFile());
  }

  private static List<Row> rows() throws Exception {
    byte[] compressed = Files.readAllBytes(REFERENCE.resolve("calendar.tsv.gz"));
    assertThat(sha(compressed)).isEqualTo(manifest().get("gzip_sha256").asText());
    byte[] content;
    try (var stream = new GZIPInputStream(new ByteArrayInputStream(compressed))) {
      content = stream.readAllBytes();
    }
    assertThat(sha(content)).isEqualTo(manifest().get("tsv_sha256").asText());
    return new String(content, StandardCharsets.UTF_8)
        .lines()
        .map(
            line -> {
              String[] values = line.split("\t");
              return new Row(
                  LocalDate.parse(values[0]),
                  new LunarDate(
                      Integer.parseInt(values[1]),
                      Integer.parseInt(values[2]),
                      Integer.parseInt(values[3]),
                      values[4].equals("1")));
            })
        .toList();
  }

  @TestFactory
  Stream<DynamicTest> allSupportedSolarDaysMatchFrozenEngineAndRoundTrip() throws Exception {
    var all = rows();
    assertThat(all).hasSize(manifest().get("days").asInt());
    assertThat(all)
        .hasSize((int) ChronoUnit.DAYS.between(LocalDate.of(1901, 1, 1), LocalDate.of(2100, 1, 1)));
    for (int index = 0; index < all.size(); index++)
      assertThat(all.get(index).solar()).isEqualTo(LocalDate.of(1901, 1, 1).plusDays(index));
    var years = all.stream().collect(Collectors.groupingBy(row -> row.solar().getYear()));
    return IntStream.rangeClosed(1901, 2099)
        .mapToObj(
            year ->
                DynamicTest.dynamicTest(
                    "calendar-" + year,
                    () -> {
                      for (var row : years.get(year)) {
                        assertThat(ZiweiCalendar.lunarDate(row.solar()))
                            .as("solar %s", row.solar())
                            .isEqualTo(row.lunar());
                        var lunar = row.lunar();
                        if (lunar.year() >= 1901) {
                          assertThat(
                                  ZiweiCalendar.solarDate(
                                      Calendar.LUNAR,
                                      lunar.year(),
                                      lunar.month(),
                                      lunar.day(),
                                      lunar.leap()))
                              .as("lunar date corresponding to %s", row.solar())
                              .isEqualTo(row.solar());
                          REVERSED_DAYS.incrementAndGet();
                        }
                        MATCHED_DAYS.incrementAndGet();
                      }
                    }));
  }

  @TestFactory
  Stream<DynamicTest> acceptsAndRejectsTheFrozenBirthCalendarCases() throws Exception {
    return StreamSupport.stream(manifest().get("birth_cases").spliterator(), false)
        .map(
            test ->
                DynamicTest.dynamicTest(
                    "birth-"
                        + test.get("calendar").asText()
                        + "-"
                        + test.get("year")
                        + "-"
                        + test.get("month")
                        + "-"
                        + test.get("day")
                        + "-"
                        + test.get("leap"),
                    () -> {
                      Calendar calendar =
                          Calendar.valueOf(test.get("calendar").asText().toUpperCase(Locale.ROOT));
                      int year = test.get("year").asInt(),
                          month = test.get("month").asInt(),
                          day = test.get("day").asInt();
                      Boolean leap =
                          test.get("leap").isNull() ? null : test.get("leap").asBoolean();
                      if (test.has("rejected")) {
                        assertThatThrownBy(
                                () -> ZiweiCalendar.solarDate(calendar, year, month, day, leap))
                            .isInstanceOf(ZiweiDates.InputError.class)
                            .hasMessage("ZIWEI_DATE_UNSUPPORTED: birth.date");
                      } else {
                        assertThat(
                                ZiweiCalendar.solarDate(calendar, year, month, day, leap)
                                    .toString())
                            .isEqualTo(test.get("solar_date").asText());
                      }
                    }));
  }

  @TestFactory
  Stream<DynamicTest> selectedTimezoneTransitionInstantsMatchPinnedPythonData() throws Exception {
    return StreamSupport.stream(manifest().get("timezones").spliterator(), false)
        .map(
            test ->
                DynamicTest.dynamicTest(
                    test.get("zone").asText(),
                    () -> {
                      var zone = ZoneId.of(test.get("zone").asText());
                      for (var sample : test.get("samples")) {
                        Instant instant = Instant.ofEpochSecond(sample.get(0).asLong());
                        assertThat(zone.getRules().getOffset(instant).getTotalSeconds())
                            .as("%s at %s", zone, instant)
                            .isEqualTo(sample.get(1).asInt());
                      }
                      MATCHED_ZONES.incrementAndGet();
                    }));
  }

  @Test
  void exactPublishedArtifactAndLicenseArePinned() throws Exception {
    var lock = JSON.readTree(Path.of("ziwei-dependencies.lock.json").toFile()).get("calendar");
    assertThat(lock.get("coordinate").asText()).isEqualTo(ZiweiCalendar.ENGINE);
    Path jar = Path.of(Solar.class.getProtectionDomain().getCodeSource().getLocation().toURI());
    assertThat(sha(Files.readAllBytes(jar)))
        .isEqualTo(lock.get("artifact_sha256").get("lunar-1.7.7.jar").asText());
    try (var license = getClass().getResourceAsStream("/META-INF/licenses/lunar-1.7.7.txt")) {
      assertThat(license).isNotNull();
      assertThat(sha(license.readAllBytes())).isEqualTo(lock.get("license_sha256").asText());
    }
  }

  @Test
  void unmodifiedLibraryDifferencesStayExplicitAndAuditable() throws Exception {
    var differences = new ArrayList<Map<String, Object>>();
    var dates = new ArrayList<LocalDate>();
    for (var row : rows()) {
      var solar = row.solar();
      var raw =
          Solar.fromYmd(solar.getYear(), solar.getMonthValue(), solar.getDayOfMonth()).getLunar();
      var actual =
          new LunarDate(raw.getYear(), Math.abs(raw.getMonth()), raw.getDay(), raw.getMonth() < 0);
      if (!actual.equals(row.lunar())) {
        dates.add(solar);
        differences.add(
            Map.of(
                "solar_date",
                solar.toString(),
                "reference",
                row.lunar(),
                "native_library",
                actual));
      }
    }
    var expectedDates =
        Stream.concat(
                LocalDate.of(1925, 5, 22).datesUntil(LocalDate.of(1925, 6, 21)),
                LocalDate.of(2057, 9, 28).datesUntil(LocalDate.of(2057, 10, 28)))
            .toList();
    assertThat(dates).containsExactlyElementsOf(expectedDates);
    Path output = Path.of("target/m6/native-calendar-differences.json");
    Files.createDirectories(output.getParent());
    JSON.writerWithDefaultPrettyPrinter()
        .writeValue(
            output.toFile(),
            Map.of(
                "calendar_engine",
                ZiweiCalendar.ENGINE,
                "reference_engine",
                "x-iztro@0.4.0",
                "unmodified_library_compatible",
                false,
                "compared_days",
                72684,
                "difference_count",
                differences.size(),
                "differences",
                differences,
                "compatibility_rules",
                ZiweiCalendar.RULES));
  }

  @Test
  void missingCalendarOrLunarLeapFlagCannotBecomeAnImplicitDefault() {
    assertThatThrownBy(() -> ZiweiCalendar.solarDate(null, 2000, 7, 17, false))
        .hasMessage("ZIWEI_INPUT_INCOMPLETE: birth.calendar");
    assertThatThrownBy(() -> ZiweiCalendar.solarDate(Calendar.LUNAR, 2000, 7, 17, null))
        .hasMessage("ZIWEI_INPUT_INCOMPLETE: birth.is_leap_month");
    assertThatThrownBy(() -> ZiweiCalendar.lunarDate(LocalDate.of(2100, 1, 1)))
        .hasMessage("ZIWEI_DATE_UNSUPPORTED: birth.date");
  }

  @Test
  void simultaneousDifferentYearsDoNotShareMutableCalendarResults() throws Exception {
    var samples =
        rows().stream()
            .filter(row -> row.solar().getDayOfMonth() == 1 && row.solar().getMonthValue() == 7)
            .toList();
    try (var executor = Executors.newFixedThreadPool(4)) {
      var jobs = new ArrayList<Future<LunarDate>>();
      for (var row : samples) jobs.add(executor.submit(() -> ZiweiCalendar.lunarDate(row.solar())));
      for (int index = 0; index < jobs.size(); index++)
        assertThat(jobs.get(index).get()).isEqualTo(samples.get(index).lunar());
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
                "Calendar and selected timezone offsets only; no Java chart engine",
                "calendar_engine",
                ZiweiCalendar.ENGINE,
                "calendar_rules",
                ZiweiCalendar.RULES,
                "reference_engine",
                "x-iztro@0.4.0",
                "matched_solar_days",
                MATCHED_DAYS.get(),
                "reversed_lunar_days",
                REVERSED_DAYS.get(),
                "matched_timezones",
                MATCHED_ZONES.get(),
                "java_tzdb",
                ZiweiDates.tzdbVersion("Asia/Shanghai"),
                "python_tzdata",
                "2026.3",
                "java_chart_engine_verified",
                false));
  }

  private static String sha(byte[] bytes) throws Exception {
    return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(bytes));
  }
}
