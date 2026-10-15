package com.financeclaw.ziwei;

import com.nlf.calendar.Lunar;
import com.nlf.calendar.Solar;
import java.time.DateTimeException;
import java.time.LocalDate;
import java.time.temporal.ChronoUnit;
import java.util.List;

/** Fixed calendar adapter; third-party calendar objects never cross this boundary. */
public final class ZiweiCalendar {
  public static final String ENGINE = "cn.6tail:lunar:1.7.7";
  public static final String RULES = "x-iztro-0.4.0-calendar-compat/1";
  private static final LocalDate FIRST = LocalDate.of(1901, 1, 1);
  private static final LocalDate LAST = LocalDate.of(2099, 12, 31);

  public enum Calendar {
    SOLAR,
    LUNAR
  }

  /** The first supported solar dates belong to lunar year 1900. */
  public record LunarDate(int year, int month, int day, boolean leap) {}

  // Preserve the frozen engine's month boundaries, not a claim about astronomical correctness.
  // Native lunar 1.7.7 differs on 1925-05-22..06-20 and 2057-09-28..10-27.
  // Include both adjacent months so forward/reverse conversion and invalid-day checks agree.
  private record LegacyMonth(int year, int month, boolean leap, LocalDate start, int days) {}

  private static final List<LegacyMonth> LEGACY_MONTHS =
      List.of(
          new LegacyMonth(1925, 4, false, LocalDate.of(1925, 4, 23), 30),
          new LegacyMonth(1925, 4, true, LocalDate.of(1925, 5, 23), 29),
          new LegacyMonth(2057, 8, false, LocalDate.of(2057, 8, 30), 29),
          new LegacyMonth(2057, 9, false, LocalDate.of(2057, 9, 28), 30));

  private ZiweiCalendar() {}

  public static LocalDate solarDate(Calendar calendar, int year, int month, int day, Boolean leap) {
    if (calendar == null) throw error("ZIWEI_INPUT_INCOMPLETE", "birth.calendar");
    if (calendar == Calendar.LUNAR && leap == null)
      throw error("ZIWEI_INPUT_INCOMPLETE", "birth.is_leap_month");
    if (year < 1901 || year > 2099 || month < 1 || month > 12 || day < 1 || day > 31)
      throw invalid();
    if (calendar == Calendar.SOLAR) {
      if (Boolean.TRUE.equals(leap)) throw invalid();
      try {
        return LocalDate.of(year, month, day);
      } catch (DateTimeException e) {
        throw invalid();
      }
    }
    // Explicit Y/M/D calls only: no default constructor, system clock or global configuration.
    for (var legacy : LEGACY_MONTHS) {
      if (legacy.year() == year && legacy.month() == month && legacy.leap() == leap) {
        if (day > legacy.days()) throw invalid();
        return legacy.start().plusDays(day - 1);
      }
    }
    LocalDate solar;
    try {
      Solar value = Lunar.fromYmd(year, leap ? -month : month, day).getSolar();
      solar = LocalDate.of(value.getYear(), value.getMonth(), value.getDay());
    } catch (IllegalArgumentException | DateTimeException e) {
      throw invalid();
    }
    checkRange(solar);
    if (!lunarDate(solar).equals(new LunarDate(year, month, day, leap))) throw invalid();
    return solar;
  }

  public static LunarDate lunarDate(LocalDate solar) {
    checkRange(solar);
    for (var legacy : LEGACY_MONTHS) {
      long day = ChronoUnit.DAYS.between(legacy.start(), solar);
      if (day >= 0 && day < legacy.days())
        return new LunarDate(legacy.year(), legacy.month(), (int) day + 1, legacy.leap());
    }
    try {
      Lunar value =
          Solar.fromYmd(solar.getYear(), solar.getMonthValue(), solar.getDayOfMonth()).getLunar();
      int month = value.getMonth();
      return new LunarDate(value.getYear(), Math.abs(month), value.getDay(), month < 0);
    } catch (IllegalArgumentException e) {
      // Third-party errors may contain submitted dates; expose a stable redacted error instead.
      throw invalid();
    }
  }

  private static void checkRange(LocalDate solar) {
    if (solar == null || solar.isBefore(FIRST) || solar.isAfter(LAST)) throw invalid();
  }

  private static ZiweiDates.InputError invalid() {
    return error("ZIWEI_DATE_UNSUPPORTED", "birth.date");
  }

  private static ZiweiDates.InputError error(String code, String field) {
    return new ZiweiDates.InputError(code, field);
  }
}
