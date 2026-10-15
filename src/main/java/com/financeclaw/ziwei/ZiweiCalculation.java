package com.financeclaw.ziwei;

import static com.financeclaw.ziwei.ZiweiInputs.map;

import com.financeclaw.agent.ResumeInputs;
import com.financeclaw.application.GovernanceService;
import java.nio.charset.StandardCharsets;
import java.time.*;
import java.time.temporal.ChronoUnit;
import java.util.*;

/**
 * Fixed facts, interval segmentation and bounded projection, independent of tools or persistence.
 */
public final class ZiweiCalculation {
  private ZiweiCalculation() {}

  public record Result(Map<String, Object> artifact, Map<String, Object> projection) {}

  public static Result calculate(
      ZiweiInputs.Normalized input,
      ZiweiDates.Level level,
      String fingerprint,
      ZiweiPolicy policy) {
    policy.verifyRuntime();
    var target = input.target();
    if ((level == ZiweiDates.Level.NATAL) != (target == null))
      throw error("ZIWEI_INPUT_INCOMPLETE");
    if (target != null && target.start().isBefore(input.birth().solarDate()))
      throw error("ZIWEI_DATE_UNSUPPORTED");
    if (target != null
        && level == ZiweiDates.Level.DAILY
        && ChronoUnit.DAYS.between(target.start(), target.end()) > 31)
      throw error("ZIWEI_RANGE_LIMIT");
    var natal = ZiweiChartEngine.natal(input.birth());
    var palaces = maps(natal.get("palaces"));
    Map<String, Map<String, Object>> facts = new LinkedHashMap<>();
    var summary = new TreeMap<String, Object>();
    for (String k :
        List.of(
            "soul",
            "body",
            "fiveElementsClass",
            "earthlyBranchOfSoulPalace",
            "earthlyBranchOfBodyPalace",
            "chineseDate")) summary.put(k, natal.get(k));
    add(facts, fact("natal", summary, null, false));
    for (var palace : palaces) {
      int index = number(palace.get("index"));
      var core = new TreeMap<String, Object>();
      core.put("index", index);
      core.put("name", palace.get("name"));
      core.put("name_key", palace.get("nameKey"));
      core.put("stem", palace.get("heavenlyStem"));
      core.put("branch", palace.get("earthlyBranch"));
      core.put("is_body", palace.get("isBodyPalace"));
      core.put("decadal_ages", map(palace.get("decadal")).get("range"));
      var stars = new ArrayList<>(maps(palace.get("majorStars")));
      stars.addAll(maps(palace.get("minorStars")));
      core.put("stars", stars(stars));
      core.put("surrounded_indices", surrounded(index));
      add(facts, fact("natal", core, index, false));
      add(facts, fact("natal", palace, index, true));
    }
    var segments = new ArrayList<Map<String, Object>>();
    if (target != null) {
      for (LocalDate day = target.start(); day.isBefore(target.end()); day = day.plusDays(1)) {
        var horoscope = ZiweiChartEngine.horoscope(input.birth(), day);
        var ids = new ArrayList<String>();
        for (int l = 1; l <= level.ordinal(); l++) {
          String layer = ZiweiDates.Level.values()[l].name().toLowerCase(Locale.ROOT);
          var raw = map(horoscope.get(layer));
          int index = number(raw.get("index"));
          if (index < 0 || index > 11 || ((List<?>) raw.get("palaceNames")).size() != 12)
            throw error("ZIWEI_DATE_UNSUPPORTED");
          var data = new TreeMap<String, Object>();
          data.put("index", index);
          data.put("stem", raw.get("heavenlyStem"));
          data.put("branch", raw.get("earthlyBranch"));
          data.put("palace_names", raw.get("palaceNames"));
          data.put("mutagens_lu_quan_ke_ji", raw.get("mutagen"));
          data.put(
              "stars_by_palace",
              ((List<?>) raw.get("stars")).stream().map(v -> stars(maps(v))).toList());
          if (l == 1) {
            var ages = (List<?>) map(palaces.get(index).get("decadal")).get("range");
            int age = number(map(horoscope.get("age")).get("nominalAge"));
            // Preserve raw engine compatibility, but never present its fallback as a valid decadal.
            if (age < number(ages.getFirst()) || age > number(ages.getLast()))
              throw error("ZIWEI_DATE_UNSUPPORTED");
            data.put("nominal_age_range", ages);
          }
          for (var f : List.of(fact(layer, data, null, false), fact(layer, raw, null, true))) {
            add(facts, f);
            ids.add((String) f.get("fact_id"));
          }
        }
        String end = day.plusDays(1).toString();
        if (!segments.isEmpty() && segments.getLast().get("fact_ids").equals(ids)) {
          var old = segments.removeLast();
          segments.add(Map.of("start", old.get("start"), "end", end, "fact_ids", List.copyOf(ids)));
        } else {
          if (segments.size() >= policy.maxSegments()) throw error("ZIWEI_RANGE_LIMIT");
          segments.add(Map.of("start", day.toString(), "end", end, "fact_ids", List.copyOf(ids)));
        }
      }
    }
    Object stableTarget =
        target == null
            ? null
            : Map.of(
                "start",
                target.start().toString(),
                "end",
                target.end().toString(),
                "timezone",
                target.clock().timezone());
    String chartId =
        "ziwei-"
            + GovernanceService.digest(
                ResumeInputs.canonical(
                    Arrays.asList(fingerprint, policy.rules(), stableTarget, level.name())));
    var artifact = new TreeMap<String, Object>();
    artifact.put("chart_id", chartId);
    artifact.put("birth_fingerprint", fingerprint);
    artifact.put("key_version", policy.keyVersion());
    artifact.put("convention", policy.rules());
    artifact.put("level", level.name().toLowerCase(Locale.ROOT));
    artifact.put("target", stableTarget);
    artifact.put("facts", List.copyOf(facts.values()));
    artifact.put("segments", List.copyOf(segments));
    artifact.put("warnings", input.warnings());
    if (bytes(artifact) > policy.artifactBytes()) throw error("ZIWEI_ARTIFACT_BUDGET_EXCEEDED");
    var selected = select(facts.values(), input.focus());
    Set<String> kept = new HashSet<>();
    selected.forEach(f -> kept.add((String) f.get("fact_id")));
    var projection = new TreeMap<String, Object>();
    projection.put("chart_id", chartId);
    projection.put("birth_fingerprint", fingerprint);
    projection.put("convention_ref", policy.convention());
    projection.put("engine_ref", policy.engine());
    projection.put("level", artifact.get("level"));
    projection.put("target", stableTarget);
    projection.put("facts", selected);
    projection.put(
        "segments",
        segments.stream()
            .map(
                s ->
                    Map.of(
                        "start",
                        s.get("start"),
                        "end",
                        s.get("end"),
                        "fact_ids",
                        ((List<?>) s.get("fact_ids")).stream().filter(kept::contains).toList()))
            .toList());
    projection.put(
        "omitted_fact_ids", facts.keySet().stream().filter(id -> !kept.contains(id)).toList());
    var warnings = new ArrayList<>(input.warnings());
    warnings.add("投影未展示完整辅助星曜；不得据此认定未展示的格局。解读须引用 fact_id 和覆盖区间。");
    projection.put("warnings", warnings);
    if (bytes(projection) > policy.projectionBytes()) throw error("ZIWEI_CONTEXT_BUDGET_EXCEEDED");
    return new Result(immutable(artifact), immutable(projection));
  }

  private static List<Map<String, Object>> select(
      Collection<Map<String, Object>> facts, String focus) {
    var core = facts.stream().filter(f -> ((String) f.get("fact_id")).contains(".core.")).toList();
    if (focus.equals("overall")) return core;
    String key =
        Map.of("career", "careerPalace", "relationship", "spousePalace", "wealth", "wealthPalace")
            .get(focus);
    String display = Map.of("career", "官禄", "relationship", "夫妻", "wealth", "财帛").get(focus);
    Set<Integer> indexes = new HashSet<>();
    for (var fact : core) {
      var data = map(fact.get("value"));
      if (fact.get("palace_index") != null) {
        if (Objects.equals(key, data.get("name_key"))
            || "soulPalace".equals(data.get("name_key"))
            || Boolean.TRUE.equals(data.get("is_body")))
          indexes.addAll(surrounded(number(fact.get("palace_index"))));
      } else if (!fact.get("layer").equals("natal")) {
        var names = (List<?>) data.get("palace_names");
        indexes.addAll(surrounded(names.indexOf(display)));
        indexes.addAll(surrounded(names.indexOf("命宫")));
      }
    }
    return core.stream()
        .filter(
            f -> f.get("palace_index") == null || indexes.contains(number(f.get("palace_index"))))
        .toList();
  }

  private static Map<String, Object> fact(
      String layer, Object value, Integer palace, boolean auxiliary) {
    var fact = new TreeMap<String, Object>();
    fact.put(
        "fact_id",
        layer
            + (auxiliary ? ".aux." : ".core.")
            + GovernanceService.digest(ResumeInputs.canonical(value)).substring(0, 20));
    fact.put("layer", layer);
    fact.put("palace_index", palace);
    fact.put("value", value);
    return fact;
  }

  private static void add(Map<String, Map<String, Object>> facts, Map<String, Object> fact) {
    facts.put((String) fact.get("fact_id"), fact);
  }

  private static List<Integer> surrounded(int index) {
    return List.of(index, (index + 4) % 12, (index + 6) % 12, (index + 8) % 12);
  }

  private static List<List<Object>> stars(List<Map<String, Object>> stars) {
    return stars.stream()
        .map(
            s ->
                List.of(
                    s.get("key"),
                    s.get("name"),
                    s.getOrDefault("brightness", ""),
                    s.getOrDefault("mutagen", "")))
        .toList();
  }

  private static int number(Object v) {
    return ((Number) v).intValue();
  }

  @SuppressWarnings("unchecked")
  private static List<Map<String, Object>> maps(Object value) {
    return (List<Map<String, Object>>) value;
  }

  public static int bytes(Object value) {
    return ResumeInputs.canonical(value).getBytes(StandardCharsets.UTF_8).length;
  }

  private static ZiweiDates.InputError error(String code) {
    return new ZiweiDates.InputError(code, "target");
  }

  @SuppressWarnings("unchecked")
  static Map<String, Object> immutable(Map<String, Object> map) {
    return (Map<String, Object>) freeze(map);
  }

  private static Object freeze(Object value) {
    if (value instanceof Map<?, ?> m) {
      var out = new TreeMap<String, Object>();
      m.forEach((k, v) -> out.put((String) k, freeze(v)));
      return Collections.unmodifiableMap(out);
    }
    if (value instanceof List<?> l) return l.stream().map(ZiweiCalculation::freeze).toList();
    return value;
  }
}
