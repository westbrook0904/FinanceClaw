package com.financeclaw.ziwei;

import java.time.*;
import java.time.temporal.ChronoUnit;
import java.time.zone.ZoneRulesProvider;
import java.util.*;

/**
 * Deterministic date handling for the five legacy chart contracts; never reads the current clock.
 */
public final class ZiweiDates {
  public enum Level {
    NATAL,
    DECADAL,
    YEARLY,
    MONTHLY,
    DAILY;

    public String toolName() {
      return "ziwei_" + name().toLowerCase(Locale.ROOT) + "_chart";
    }

    public static Level forTool(String name) {
      return Arrays.stream(values())
          .filter(level -> level.toolName().equals(name))
          .findFirst()
          .orElseThrow(() -> invalid("tool"));
    }
  }

  /** Supplied by the execution layer, never by model arguments. */
  public record QueryClock(Instant requestClock, String timezone) {
    public QueryClock {
      if (requestClock == null) throw missing("request_clock");
      zone(timezone);
    }
  }

  public record Target(LocalDate start, LocalDate end, QueryClock clock) {
    public Target {
      if (start == null || end == null || clock == null) throw missing("target");
      long days = ChronoUnit.DAYS.between(start, end);
      if (days < 1 || days > 366 || start.getYear() < 1901 || end.minusDays(1).getYear() > 2099)
        throw new InputError("ZIWEI_RANGE_LIMIT", "target");
    }
  }

  public record CivilTime(String shichen, Instant utcInstant, String precision) {}

  /** Contains field paths and stable codes only, never the submitted birth data. */
  public static final class InputError extends IllegalArgumentException {
    private final String code;
    private final String field;

    InputError(String code, String field) {
      super(code + ": " + field);
      this.code = code;
      this.field = field;
    }

    public String code() {
      return code;
    }

    public String field() {
      return field;
    }
  }

  static final List<String> SHICHEN =
      List.of(
          "zi_early",
          "chou",
          "yin",
          "mao",
          "chen",
          "si",
          "wu",
          "wei",
          "shen",
          "you",
          "xu",
          "hai",
          "zi_late");
  private static final Set<String> COMMON =
      Set.of("question", "subject_label", "mode", "birth", "focus");

  private ZiweiDates() {}

  /** Validates date fields only. Birth/profile validation and chart computation are separate. */
  public static Target resolve(String tool, Map<String, Object> arguments, QueryClock clock) {
    Level level = Level.forTool(tool);
    if (arguments == null) throw missing("arguments");
    String offsetKey =
        switch (level) {
          case YEARLY -> "year_offset";
          case MONTHLY -> "month_offset";
          default -> "day_offset";
        };
    Set<String> allowed = new HashSet<>(COMMON);
    if (level != Level.NATAL) allowed.addAll(Set.of("on_date", "date_range", offsetKey));
    if (level == Level.YEARLY || level == Level.MONTHLY) allowed.add("year");
    if (level == Level.MONTHLY) allowed.add("month");
    if (!allowed.containsAll(arguments.keySet())) throw invalid("arguments");
    if (level == Level.NATAL) return null;
    if (clock == null) throw missing("request_clock");
    boolean point = arguments.get("on_date") != null;
    boolean range = arguments.get("date_range") != null;
    boolean offset = arguments.get(offsetKey) != null;
    boolean period = arguments.get("year") != null || arguments.get("month") != null;
    int forms = (point ? 1 : 0) + (range ? 1 : 0) + (offset ? 1 : 0) + (period ? 1 : 0);
    if (forms == 0) throw missing("target");
    if (forms > 1) throw invalid("target");
    try {
      LocalDate start, end;
      if (point) {
        start = date(arguments.get("on_date"), "on_date");
        end = start.plusDays(1);
      } else if (range) {
        if (!(arguments.get("date_range") instanceof Map<?, ?> interval))
          throw invalid("date_range");
        if (!Set.of("start", "end").containsAll(interval.keySet())) throw invalid("date_range");
        start = date(interval.get("start"), "date_range.start");
        end = date(interval.get("end"), "date_range.end");
      } else {
        LocalDate today = clock.requestClock().atZone(zone(clock.timezone())).toLocalDate();
        int amount = offset ? integer(arguments.get(offsetKey), offsetKey, -10, 10) : 0;
        if (level == Level.YEARLY) {
          int year =
              offset
                  ? today.getYear() + amount
                  : integer(arguments.get("year"), "year", 1901, 2099);
          start = LocalDate.of(year, 1, 1);
          end = start.plusYears(1);
        } else if (level == Level.MONTHLY) {
          start =
              offset
                  ? today.withDayOfMonth(1).plusMonths(amount)
                  : LocalDate.of(
                      integer(arguments.get("year"), "year", 1901, 2099),
                      integer(arguments.get("month"), "month", 1, 12),
                      1);
          end = start.plusMonths(1);
        } else {
          start = today.plusDays(amount);
          end = start.plusDays(1);
        }
      }
      return new Target(start, end, clock);
    } catch (DateTimeException | ArithmeticException e) {
      throw new InputError("ZIWEI_RANGE_LIMIT", "target");
    }
  }

  /** The solar date must already have been validated by the selected calendar engine. */
  public static CivilTime civilTime(
      LocalDate solarDate, String timezone, Map<String, Object> input) {
    if (solarDate == null || solarDate.getYear() < 1901 || solarDate.getYear() > 2099)
      throw new InputError("ZIWEI_DATE_UNSUPPORTED", "birth.date");
    ZoneId zone = zone(timezone);
    if (input == null) throw missing("birth.time");
    if (!Set.of("kind", "clock", "end", "shichen", "fold").containsAll(input.keySet()))
      throw invalid("birth.time");
    String kind = input.getOrDefault("kind", "unknown") instanceof String value ? value : "invalid";
    Object startValue = input.get("clock"),
        endValue = input.get("end"),
        slot = input.get("shichen"),
        foldValue = input.get("fold");
    if (!Set.of("unknown", "shichen", "clock", "range").contains(kind))
      throw invalid("birth.time.kind");
    if (kind.equals("unknown")) {
      if (startValue != null || endValue != null || slot != null || foldValue != null)
        throw invalid("birth.time");
      throw missing("birth.time");
    }
    if (kind.equals("shichen")) {
      if (startValue != null || endValue != null || foldValue != null) throw invalid("birth.time");
      if (slot == null || slot.equals("zi")) throw ambiguous();
      if (!(slot instanceof String value) || !SHICHEN.contains(value))
        throw invalid("birth.time.shichen");
      return new CivilTime(value, null, kind);
    }
    if (slot != null || (kind.equals("clock") && endValue != null)) throw invalid("birth.time");
    int fold = foldValue == null ? -1 : integer(foldValue, "birth.time.fold", 0, 1);
    LocalTime start = time(startValue, "birth.time.clock");
    LocalTime end = kind.equals("range") ? time(endValue, "birth.time.end") : start;
    if (end.isBefore(start)) throw ambiguous();
    String shichen = SHICHEN.get((start.getHour() + 1) / 2);
    Instant instant = null;
    // Validate every minute of a closed interval, including DST transitions inside the range.
    for (int minute = start.toSecondOfDay() / 60; minute <= end.toSecondOfDay() / 60; minute++) {
      LocalDateTime local = solarDate.atTime(minute / 60, minute % 60);
      var offsets = zone.getRules().getValidOffsets(local);
      if (offsets.isEmpty()
          || (offsets.size() > 1 && fold < 0)
          || !SHICHEN.get((local.getHour() + 1) / 2).equals(shichen)) throw ambiguous();
      if (kind.equals("clock"))
        instant = local.toInstant(offsets.get(offsets.size() == 1 ? 0 : fold));
    }
    return new CivilTime(shichen, instant, kind);
  }

  public static String tzdbVersion(String name) {
    zone(name);
    return ZoneRulesProvider.getVersions(name).lastKey();
  }

  private static ZoneId zone(String name) {
    if (name == null || !ZoneId.getAvailableZoneIds().contains(name))
      throw new InputError("ZIWEI_TIME_AMBIGUOUS", "timezone");
    return ZoneId.of(name);
  }

  private static LocalDate date(Object value, String field) {
    if (value == null) throw missing(field);
    if (!(value instanceof String text) || !text.matches("[0-9]{4}-[0-9]{2}-[0-9]{2}"))
      throw invalid(field);
    try {
      return LocalDate.parse(text);
    } catch (DateTimeException e) {
      throw invalid(field);
    }
  }

  private static LocalTime time(Object value, String field) {
    if (value == null) throw missing(field);
    if (!(value instanceof String text) || !text.matches("(?:[01][0-9]|2[0-3]):[0-5][0-9]"))
      throw invalid(field);
    return LocalTime.parse(text);
  }

  private static int integer(Object value, String field, int min, int max) {
    if (value == null) throw missing(field);
    if (!(value instanceof Byte
        || value instanceof Short
        || value instanceof Integer
        || value instanceof Long)) throw invalid(field);
    long number = ((Number) value).longValue();
    if (number < min || number > max) throw invalid(field);
    return (int) number;
  }

  private static InputError missing(String field) {
    return new InputError("ZIWEI_INPUT_INCOMPLETE", field);
  }

  private static InputError invalid(String field) {
    return new InputError("ZIWEI_TOOL_INPUT_INVALID", field);
  }

  private static InputError ambiguous() {
    return new InputError("ZIWEI_TIME_AMBIGUOUS", "birth.time");
  }
}
