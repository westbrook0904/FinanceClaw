package com.financeclaw.m6;

import static org.assertj.core.api.Assertions.*;

import com.fasterxml.jackson.databind.*;
import com.fasterxml.jackson.databind.node.*;
import com.financeclaw.ziwei.*;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.security.MessageDigest;
import java.time.LocalDate;
import java.util.*;
import java.util.concurrent.*;
import java.util.stream.*;
import java.util.zip.GZIPInputStream;
import org.junit.jupiter.api.*;

class ZiweiChartEngineTest {
  private static final ObjectMapper JSON = new ObjectMapper();
  private static final Path REFERENCE = Path.of("src/test/resources/ziwei/reference");
  private static final Map<String, JsonNode> MATCHED = new ConcurrentHashMap<>();
  private static int expandedMatched;
  private static final Path EXPANDED = Path.of("src/test/resources/ziwei/charts");

  static ZiweiChartEngine.Birth birth(JsonNode row) {
    var input = row.get("input");
    var ymd = input.get("date");
    var date =
        ZiweiCalendar.solarDate(
            ZiweiCalendar.Calendar.valueOf(input.get("calendar").asText().toUpperCase(Locale.ROOT)),
            ymd.get(0).asInt(),
            ymd.get(1).asInt(),
            ymd.get(2).asInt(),
            input.get("is_leap_month").asBoolean());
    return ZiweiChartEngine.Birth.of(
        date, input.get("shichen").asText(), input.get("sex_for_chart").asText());
  }

  static ObjectNode calculate(JsonNode row) {
    var birth = birth(row);
    var output = JSON.createObjectNode();
    output.set("id", row.get("id"));
    output.set("input", row.get("input").deepCopy());
    output.put("solar_date", birth.solarDate().toString());
    output.set("natal", JSON.valueToTree(ZiweiChartEngine.natal(birth)));
    output.set(
        "horoscope",
        JSON.valueToTree(
            ZiweiChartEngine.horoscope(
                birth, LocalDate.parse(row.get("input").get("target").asText()))));
    return output;
  }

  static List<String> differences(JsonNode expected, JsonNode actual, String path) {
    if (expected.equals(actual)) return List.of();
    if (expected.getNodeType() != actual.getNodeType()) return List.of(path);
    var differences = new ArrayList<String>();
    if (expected.isObject()) {
      var fields = new TreeSet<String>();
      expected.fieldNames().forEachRemaining(fields::add);
      actual.fieldNames().forEachRemaining(fields::add);
      for (String field : fields)
        differences.addAll(
            differences(expected.path(field), actual.path(field), path + "/" + field));
    } else if (expected.isArray() && expected.size() == actual.size()) {
      for (int i = 0; i < expected.size(); i++)
        differences.addAll(differences(expected.get(i), actual.get(i), path + "/" + i));
    } else differences.add(path);
    return differences;
  }

  private static JsonNode originalCases() throws Exception {
    byte[] bytes = Files.readAllBytes(REFERENCE.resolve("charts.json"));
    var manifest = JSON.readTree(REFERENCE.resolve("manifest.json").toFile());
    assertThat(HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(bytes)))
        .isEqualTo(manifest.get("fixture_sha256").get("charts.json").asText());
    return JSON.readTree(bytes);
  }

  @TestFactory
  Stream<DynamicTest> completeFrozenChartsMatchEveryFieldAndArrayOrder() throws Exception {
    return StreamSupport.stream(originalCases().spliterator(), false)
        .map(
            row ->
                DynamicTest.dynamicTest(
                    row.get("id").asText(),
                    () -> {
                      var actual = calculate(row);
                      assertThat(differences(row, actual, row.get("id").asText())).isEmpty();
                      MATCHED.put(row.get("id").asText(), actual);
                    }));
  }

  @Test
  void expandedCorpusMatchesFullRawChartsIncludingEveryAgeAndCalendarBoundary() throws Exception {
    var manifest = JSON.readTree(EXPANDED.resolve("manifest.json").toFile());
    byte[] compressed = Files.readAllBytes(EXPANDED.resolve("charts.jsonl.gz"));
    assertThat(sha(compressed)).isEqualTo(manifest.get("gzip_sha256").asText());
    var digest = MessageDigest.getInstance("SHA-256");
    var ids = new HashSet<String>();
    var mismatches = new ArrayList<String>();
    var elements = new HashSet<String>();
    var birthYears = new HashSet<JsonNode>();
    var targetYears = new HashSet<String>();
    var times = new HashSet<Integer>();
    var sexes = new HashSet<String>();
    var groups = new TreeMap<String, Integer>();
    try (var reader =
        new BufferedReader(
            new InputStreamReader(
                new GZIPInputStream(new ByteArrayInputStream(compressed)),
                StandardCharsets.UTF_8))) {
      String line;
      while ((line = reader.readLine()) != null) {
        digest.update((line + "\n").getBytes(StandardCharsets.UTF_8));
        var row = JSON.readTree(line);
        String id = row.get("id").asText();
        assertThat(ids.add(id)).as("unique synthetic case id").isTrue();
        var actual = calculate(row);
        var differences = differences(row, actual, id);
        if (differences.isEmpty()) expandedMatched++;
        else if (mismatches.size() < 50)
          mismatches.addAll(differences.stream().limit(50 - mismatches.size()).toList());
        elements.add(actual.get("natal").get("fiveElementsClassKey").asText());
        birthYears.add(actual.get("natal").get("rawDates").get("chineseDate").get("yearlyKeys"));
        var yearly = actual.get("horoscope").get("yearly");
        targetYears.add(
            yearly.get("heavenlyStemKey").asText() + yearly.get("earthlyBranchKey").asText());
        times.add(actual.get("natal").get("timeIndex").asInt());
        sexes.add(actual.get("natal").get("genderKey").asText());
        groups.merge(id.substring(0, id.lastIndexOf('-')), 1, Integer::sum);
      }
    }
    assertThat(HexFormat.of().formatHex(digest.digest()))
        .isEqualTo(manifest.get("jsonl_sha256").asText());
    assertThat(ids).hasSize(manifest.get("cases").asInt()).hasSize(2515);
    assertThat((JsonNode) JSON.valueToTree(groups)).isEqualTo(manifest.get("groups"));
    assertThat(elements).hasSize(5);
    assertThat(birthYears).hasSize(60);
    assertThat(targetYears).hasSize(60);
    assertThat(times).hasSize(13);
    assertThat(sexes).containsExactlyInAnyOrder("male", "female");
    assertThat(mismatches).isEmpty();
    assertThat(expandedMatched).isEqualTo(ids.size());
  }

  @Test
  void implementationAndNoticesHaveExplicitIndependentIdentity() throws Exception {
    var lock =
        JSON.readTree(Path.of("ziwei-dependencies.lock.json").toFile()).get("algorithm_reference");
    assertThat(lock.get("implementation").asText()).isEqualTo(ZiweiChartEngine.ENGINE);
    assertThat(lock.get("commit").asText()).isEqualTo("cd14dfd8cf18dcc1ab248ba3c20c86903f5bc07f");
    assertThat(lock.get("adopted").asBoolean()).isTrue();
    for (var notice : List.of(lock, lock.get("upstream_notice"))) {
      String path = notice.get("license_path").asText().replace("src/main/resources", "");
      try (var stream = getClass().getResourceAsStream(path)) {
        assertThat(stream).isNotNull();
        assertThat(sha(stream.readAllBytes())).isEqualTo(notice.get("license_sha256").asText());
      }
    }
  }

  @Test
  void rejectsMissingOrInvalidInputsWithoutDefaultingTheDateOrTime() {
    assertThatThrownBy(() -> ZiweiChartEngine.natal(null))
        .hasMessage("ZIWEI_INPUT_INCOMPLETE: birth");
    assertThatThrownBy(() -> ZiweiChartEngine.Birth.of(LocalDate.of(2000, 1, 1), "zi", "male"))
        .hasMessage("ZIWEI_TIME_AMBIGUOUS: birth.time");
    assertThatThrownBy(() -> ZiweiChartEngine.Birth.of(LocalDate.of(2000, 1, 1), "yin", "unknown"))
        .hasMessage("ZIWEI_TOOL_INPUT_INVALID: birth.sex_for_chart");
    assertThatThrownBy(
            () ->
                new ZiweiChartEngine.Birth(LocalDate.of(2000, 1, 1), 13, ZiweiChartEngine.Sex.MALE))
        .hasMessage("ZIWEI_TIME_AMBIGUOUS: birth.time");
    var birth = ZiweiChartEngine.Birth.of(LocalDate.of(2000, 1, 1), "yin", "female");
    assertThatThrownBy(() -> ZiweiChartEngine.horoscope(birth, null))
        .hasMessage("ZIWEI_DATE_UNSUPPORTED: target");
    assertThatThrownBy(() -> ZiweiChartEngine.horoscope(birth, LocalDate.of(2100, 1, 1)))
        .hasMessage("ZIWEI_DATE_UNSUPPORTED: target");
  }

  @Test
  @SuppressWarnings("unchecked")
  void callersCannotMutateNestedChartsOrLeakStateIntoAnotherCall() {
    var birth = ZiweiChartEngine.Birth.of(LocalDate.of(2000, 8, 16), "yin", "female");
    var chart = ZiweiChartEngine.natal(birth);
    var palaces = (List<Map<String, Object>>) chart.get("palaces");
    var major = (List<Map<String, Object>>) palaces.get(4).get("majorStars");
    assertThatThrownBy(() -> chart.put("gender", "男"))
        .isInstanceOf(UnsupportedOperationException.class);
    assertThatThrownBy(() -> palaces.clear()).isInstanceOf(UnsupportedOperationException.class);
    assertThatThrownBy(() -> major.getFirst().put("name", "changed"))
        .isInstanceOf(UnsupportedOperationException.class);
    assertThat(ZiweiChartEngine.natal(birth)).isEqualTo(chart);
  }

  @Test
  void concurrentDifferentBirthsAndTargetsMatchIndependentFrozenResults() throws Exception {
    var rows = originalCases();
    try (var executor = Executors.newFixedThreadPool(8)) {
      var tasks = new ArrayList<Future<List<String>>>();
      for (int round = 0; round < 8; round++)
        for (var row : rows)
          tasks.add(
              executor.submit(() -> differences(row, calculate(row), row.get("id").asText())));
      for (var result : tasks) assertThat(result.get(30, TimeUnit.SECONDS)).isEmpty();
    }
  }

  @AfterAll
  static void saveCandidate() throws Exception {
    Files.createDirectories(Path.of("target/m6"));
    JSON.writerWithDefaultPrettyPrinter()
        .writeValue(
            Path.of("target/m6/java-charts.json").toFile(),
            MATCHED.values().stream()
                .sorted(Comparator.comparing(row -> row.get("id").asText()))
                .toList());
    var manifest = JSON.readTree(EXPANDED.resolve("manifest.json").toFile());
    JSON.writerWithDefaultPrettyPrinter()
        .writeValue(
            Path.of("target/m6/chart-results.json").toFile(),
            Map.of(
                "engine",
                ZiweiChartEngine.ENGINE,
                "calendar_rules",
                ZiweiCalendar.RULES,
                "original_charts_matched",
                MATCHED.size(),
                "expanded_charts_matched",
                expandedMatched,
                "expanded_reference_sha256",
                manifest.get("gzip_sha256").asText(),
                "scope",
                "Fixed default Chinese raw charts only; no product governance, native toolkit registration or real provider execution",
                "java_chart_engine_verified",
                MATCHED.size() == 9 && expandedMatched == 2515));
  }

  private static String sha(byte[] value) throws Exception {
    return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(value));
  }
}
