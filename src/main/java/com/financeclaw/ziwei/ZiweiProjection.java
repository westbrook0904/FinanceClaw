package com.financeclaw.ziwei;

import static com.financeclaw.ziwei.ZiweiValues.bytes;
import static com.financeclaw.ziwei.ZiweiValues.map;

import com.financeclaw.domain.release.ZiweiPolicy;
import java.util.*;

/** Focus selection and bounded model projection; complete artifact facts remain unchanged. */
final class ZiweiProjection {
  private ZiweiProjection() {}

  static Map<String, Object> project(
      ZiweiInputs.Normalized input,
      ZiweiDates.Level level,
      String fingerprint,
      ZiweiPolicy policy,
      String chartId,
      Object stableTarget,
      Map<String, Map<String, Object>> facts,
      List<Map<String, Object>> segments) {
    var selected = select(facts.values(), input.focus());
    Set<String> kept = new HashSet<>();
    selected.forEach(f -> kept.add((String) f.get("fact_id")));
    var projection = new TreeMap<String, Object>();
    projection.put("chart_id", chartId);
    projection.put("birth_fingerprint", fingerprint);
    projection.put("convention_ref", policy.convention());
    projection.put("engine_ref", policy.engine());
    projection.put("level", level.name().toLowerCase(Locale.ROOT));
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
    if (bytes(projection) > policy.projectionBytes())
      throw new ZiweiDates.InputError("ZIWEI_CONTEXT_BUDGET_EXCEEDED", "target");
    return ZiweiValues.immutable(projection);
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

  private static List<Integer> surrounded(int index) {
    return List.of(index, (index + 4) % 12, (index + 6) % 12, (index + 8) % 12);
  }

  private static int number(Object v) {
    return ((Number) v).intValue();
  }
}
