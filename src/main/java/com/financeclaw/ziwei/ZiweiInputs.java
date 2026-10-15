package com.financeclaw.ziwei;

import com.financeclaw.agent.ResumeInputs;
import com.financeclaw.application.GovernanceService;
import com.financeclaw.domain.Task.Owner;
import io.agentscope.core.tool.ToolValidator;
import java.nio.charset.StandardCharsets;
import java.time.*;
import java.util.*;
import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;

/** Aggregated birth clarification and deterministic normalization; no shared user state. */
public final class ZiweiInputs {
  private static final String RESOURCE =
      com.financeclaw.tools.BusinessCatalog.read("classpath:ziwei/inputs.json");
  private static final Map<String, Object> DOCUMENT =
      ZiweiCalculation.immutable(GovernanceService.object(RESOURCE));
  private static final Map<String, List<String>> CITIES =
      Map.of(
          "北京",
          List.of("CN", "Asia/Shanghai"),
          "上海",
          List.of("CN", "Asia/Shanghai"),
          "广州",
          List.of("CN", "Asia/Shanghai"),
          "深圳",
          List.of("CN", "Asia/Shanghai"),
          "成都",
          List.of("CN", "Asia/Shanghai"),
          "香港",
          List.of("HK", "Asia/Hong_Kong"),
          "台北",
          List.of("TW", "Asia/Taipei"));
  private static final Map<String, String> LABELS =
      Map.ofEntries(
          Map.entry("birth.calendar", "出生历法（公历或农历）"),
          Map.entry("birth.date", "完整出生年月日"),
          Map.entry("birth.date.year", "出生年份"),
          Map.entry("birth.date.month", "出生月份"),
          Map.entry("birth.date.day", "出生日期中的日"),
          Map.entry("birth.is_leap_month", "农历月份是否为闰月"),
          Map.entry("birth.time", "明确出生时间或时辰（子时须区分早晚；夏令时须消除歧义）"),
          Map.entry("birth.time.clock", "出生钟表时间"),
          Map.entry("birth.time.end", "时间区间结束时间"),
          Map.entry("birth.time_basis", "出生记录时制（当前仅支持民用钟表时间）"),
          Map.entry("birth.place", "相互一致的出生地点、地区与时区"),
          Map.entry("birth.place.name", "出生地名称"),
          Map.entry("birth.place.timezone", "出生地 IANA 时区"),
          Map.entry("birth.sex_for_chart", "用户提供的排盘性别"),
          Map.entry("target", "明确查询日期或半开区间"),
          Map.entry("year", "查询年份"),
          Map.entry("month", "查询月份"),
          Map.entry("date_range.start", "查询区间开始日期"),
          Map.entry("date_range.end", "查询区间结束日期"));

  private ZiweiInputs() {}

  public record Issue(String field, String code) {}

  public record Normalized(
      ZiweiChartEngine.Birth birth,
      String timezone,
      ZiweiDates.CivilTime time,
      String birthJson,
      ZiweiDates.Target target,
      String focus,
      String mode,
      List<String> warnings) {
    public Normalized {
      warnings = List.copyOf(warnings);
    }

    public String fingerprint(Owner owner, ZiweiPolicy policy, byte[] key) {
      return hmac(
          key,
          List.of(
              owner.tenant(),
              owner.subject(),
              birthJson,
              birth.solarDate().toString(),
              time.shichen(),
              timezone,
              policy.rules(),
              policy.keyVersion()));
    }
  }

  public record Review(Normalized value, List<Issue> issues) {
    public Review {
      issues = List.copyOf(issues);
    }

    public boolean complete() {
      return value != null && issues.isEmpty();
    }

    public String question() {
      return "请补充或确认以下资料，不必提供街道地址：\n"
          + issues.stream()
              .map(
                  i ->
                      "- "
                          + LABELS.getOrDefault(i.field(), "排盘参数格式或查询范围")
                          + (i.code().equals("ZIWEI_INPUT_INCOMPLETE") ? "（请补充）" : "（请核对）"))
              .distinct()
              .collect(java.util.stream.Collectors.joining("\n"));
    }
  }

  public static String schemaHash() {
    return GovernanceService.digest(RESOURCE);
  }

  public static boolean supports(String tool) {
    return Arrays.stream(ZiweiDates.Level.values()).anyMatch(l -> l.toolName().equals(tool));
  }

  public static String description(String tool) {
    return "固定紫微"
        + ZiweiDates.Level.forTool(tool).name()
        + "盘面。填写用户已知出生资料，未知留空并澄清，禁止猜测。"
        + "流运须有明确日期/期间，偏移按服务端固定时间解析。chart_only 只展示事实；解读须引用 fact_id 和区间，"
        + "不得推断省略的星曜或将传统文化结果作为事实预测。完整事实使用返回的 Artifact 引用读取。";
  }

  public static Map<String, Object> schema(String tool) {
    ZiweiDates.Level.forTool(tool);
    var properties = new TreeMap<>(map(DOCUMENT.get("common")));
    properties.putAll(map(map(DOCUMENT.get("targets")).get(tool)));
    return Map.of(
        "type",
        "object",
        "$defs",
        DOCUMENT.get("definitions"),
        "properties",
        Collections.unmodifiableMap(properties),
        "additionalProperties",
        false);
  }

  /** Only birth and date corrections are accepted; root identity/intent cannot be injected. */
  public static Map<String, Object> answerSchema(String tool) {
    var schema = new TreeMap<>(schema(tool));
    var properties = new TreeMap<>(map(schema.get("properties")));
    properties.keySet().removeAll(Set.of("question", "subject_label", "focus", "mode"));
    schema.put("properties", Collections.unmodifiableMap(properties));
    schema.put("minProperties", 1);
    return Collections.unmodifiableMap(schema);
  }

  public static Map<String, Object> corrected(
      String tool, Map<String, Object> input, Object answer) {
    if (ToolValidator.validateInput(ResumeInputs.canonical(answer), answerSchema(tool)) != null)
      throw new IllegalArgumentException("Invalid Ziwei clarification");
    Map<String, Object> patch = map(answer);
    var merged = new TreeMap<>(input);
    Set<String> targets = map(map(DOCUMENT.get("targets")).get(tool)).keySet();
    // A new date selector replaces the old selector, not silently combines two forms.
    if (patch.keySet().stream().anyMatch(targets::contains)) {
      boolean sameForm =
          (patch.containsKey("date_range") && input.containsKey("date_range"))
              || ((patch.containsKey("year") || patch.containsKey("month"))
                  && (input.containsKey("year") || input.containsKey("month")));
      if (!sameForm) targets.forEach(merged::remove);
    }
    return merge(merged, patch);
  }

  private static Map<String, Object> merge(
      Map<String, Object> original, Map<String, Object> patch) {
    var result = new TreeMap<>(original);
    // Changing precision replaces the time representation; keep same-kind partial corrections.
    if (patch.containsKey("kind") && !Objects.equals(patch.get("kind"), original.get("kind")))
      return ZiweiCalculation.immutable(patch);
    patch.forEach(
        (k, v) ->
            result.put(
                k,
                v instanceof Map<?, ?> && original.get(k) instanceof Map<?, ?>
                    ? merge(map(original.get(k)), map(v))
                    : v));
    return Collections.unmodifiableMap(result);
  }

  public static Review inspect(
      String tool, Map<String, Object> input, ZiweiDates.QueryClock clock) {
    var issues = new ArrayList<Issue>();
    if (input == null
        || ToolValidator.validateInput(ResumeInputs.canonical(input), schema(tool)) != null)
      return new Review(null, List.of(new Issue("arguments", "ZIWEI_TOOL_INPUT_INVALID")));
    var birth = map(input.get("birth"));
    for (String key : List.of("calendar", "date", "place", "sex_for_chart"))
      if (birth.get(key) == null) missing(issues, "birth." + key);
    Object basis = birth.getOrDefault("time_basis", "civil");
    if (basis == null) missing(issues, "birth.time_basis");
    else if (!"civil".equals(basis))
      issues.add(new Issue("birth.time_basis", "ZIWEI_CONVENTION_UNSUPPORTED"));
    String zone = null;
    var place = map(birth.get("place"));
    if (birth.get("place") != null) {
      String name = Objects.toString(place.get("name"), "").trim();
      if (name.isEmpty()) missing(issues, "birth.place.name");
      var known = CITIES.get(name.endsWith("市") ? name.substring(0, name.length() - 1) : name);
      zone = (String) place.get("timezone");
      if (known != null) {
        if ((zone != null && !known.get(1).equals(zone))
            || (place.get("country_code") != null
                && !known.getFirst().equals(place.get("country_code"))))
          issues.add(new Issue("birth.place", "ZIWEI_PLACE_AMBIGUOUS"));
        zone = known.get(1);
      }
      if (zone == null || !ZoneId.getAvailableZoneIds().contains(zone)) {
        issues.add(new Issue("birth.place.timezone", "ZIWEI_PLACE_AMBIGUOUS"));
        zone = null;
      }
    }
    var date = map(birth.get("date"));
    if (birth.get("date") != null)
      for (String k : List.of("year", "month", "day"))
        if (date.get(k) == null) missing(issues, "birth.date." + k);
    if ("lunar".equals(birth.get("calendar")) && birth.get("is_leap_month") == null)
      missing(issues, "birth.is_leap_month");
    LocalDate solar = null;
    if (birth.get("calendar") != null
        && date.keySet().containsAll(List.of("year", "month", "day"))) {
      try {
        solar =
            ZiweiCalendar.solarDate(
                ZiweiCalendar.Calendar.valueOf(
                    ((String) birth.get("calendar")).toUpperCase(Locale.ROOT)),
                integer(date.get("year")),
                integer(date.get("month")),
                integer(date.get("day")),
                (Boolean) birth.get("is_leap_month"));
      } catch (ZiweiDates.InputError e) {
        add(issues, e);
      }
    }
    var timeInput = map(birth.get("time"));
    ZiweiDates.CivilTime time = null;
    if (Set.of("clock", "range").contains(timeInput.getOrDefault("kind", "unknown"))) {
      if (timeInput.get("clock") == null) missing(issues, "birth.time.clock");
      if ("range".equals(timeInput.get("kind")) && timeInput.get("end") == null)
        missing(issues, "birth.time.end");
    }
    if (timeInput.isEmpty() || "unknown".equals(timeInput.getOrDefault("kind", "unknown")))
      missing(issues, "birth.time");
    else if (solar != null && zone != null) {
      try {
        time = ZiweiDates.civilTime(solar, zone, timeInput);
      } catch (ZiweiDates.InputError e) {
        add(issues, e);
      }
    }
    ZiweiDates.Target target = null;
    try {
      target = ZiweiDates.resolve(tool, input, clock);
    } catch (ZiweiDates.InputError e) {
      add(issues, e);
    }
    if (!issues.isEmpty()) return new Review(null, issues.stream().distinct().toList());
    var warnings = new ArrayList<String>();
    warnings.add("固定候选规则尚待独立核验；仅供传统文化参考。不能替代财务、医疗或其他专业判断。");
    if (!time.precision().equals("clock")) warnings.add("仅保留时辰或区间精度，不生成精确 UTC 出生时刻。");
    if (solar.getYear() < 1970) warnings.add("早期历史时区资料可能不完整，请核对出生记录。");
    String name = Objects.toString(place.get("name"), "").replaceAll("市$", "");
    if (!CITIES.containsKey(name)) warnings.add("时区采用用户明确提供的值，未进行地理编码核验。");
    return new Review(
        new Normalized(
            ZiweiChartEngine.Birth.of(solar, time.shichen(), (String) birth.get("sex_for_chart")),
            zone,
            time,
            ResumeInputs.canonical(birth),
            target,
            (String) input.getOrDefault("focus", "overall"),
            (String) input.getOrDefault("mode", "interpretation"),
            warnings),
        List.of());
  }

  private static int integer(Object v) {
    if (!(v instanceof Byte || v instanceof Short || v instanceof Integer || v instanceof Long))
      throw new ZiweiDates.InputError("ZIWEI_TOOL_INPUT_INVALID", "birth.date");
    return ((Number) v).intValue();
  }

  private static void missing(List<Issue> issues, String field) {
    issues.add(new Issue(field, "ZIWEI_INPUT_INCOMPLETE"));
  }

  private static void add(List<Issue> issues, ZiweiDates.InputError e) {
    issues.add(new Issue(e.field(), e.code()));
  }

  @SuppressWarnings("unchecked")
  static Map<String, Object> map(Object value) {
    return value instanceof Map<?, ?> ? (Map<String, Object>) value : Map.of();
  }

  public static String hmac(byte[] key, Object value) {
    try {
      var mac = Mac.getInstance("HmacSHA256");
      mac.init(new SecretKeySpec(key, "HmacSHA256"));
      return HexFormat.of()
          .formatHex(mac.doFinal(ResumeInputs.canonical(value).getBytes(StandardCharsets.UTF_8)));
    } catch (java.security.GeneralSecurityException e) {
      throw new IllegalStateException("HMAC unavailable");
    }
  }
}
