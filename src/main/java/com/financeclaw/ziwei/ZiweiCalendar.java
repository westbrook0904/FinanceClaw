package com.financeclaw.ziwei;

import com.nlf.calendar.Lunar;
import com.nlf.calendar.LunarMonth;
import com.nlf.calendar.Solar;
import java.time.DateTimeException;
import java.time.LocalDate;

/** Fixed calendar adapter; third-party calendar objects never cross this boundary. */
public final class ZiweiCalendar {
    public static final String ENGINE = "cn.6tail:lunar:1.7.7";
    public static final String RULES = "native-lunar-1.7.7/1";
    private static final LocalDate FIRST = LocalDate.of(1901, 1, 1);
    private static final LocalDate LAST = LocalDate.of(2099, 12, 31);

    public enum Calendar {
        SOLAR,
        LUNAR
    }

    /** The first supported solar dates belong to lunar year 1900. */
    public record LunarDate(int year, int month, int day, boolean leap) {}

    private ZiweiCalendar() {}

    public static LocalDate solarDate(
            Calendar calendar, int year, int month, int day, Boolean leap) {
        if (calendar == null) {
            throw error("ZIWEI_INPUT_INCOMPLETE", "birth.calendar");
        }
        if (calendar == Calendar.LUNAR && leap == null) {
            throw error("ZIWEI_INPUT_INCOMPLETE", "birth.is_leap_month");
        }
        if (year < 1901 || year > 2099 || month < 1 || month > 12 || day < 1 || day > 31) {
            throw invalid();
        }
        if (calendar == Calendar.SOLAR) {
            if (Boolean.TRUE.equals(leap)) {
                throw invalid();
            }
            try {
                return LocalDate.of(year, month, day);
            } catch (DateTimeException e) {
                throw invalid();
            }
        }
        // Explicit Y/M/D calls only: no default constructor, system clock or global configuration.
        LocalDate solar;
        try {
            Solar value = Lunar.fromYmd(year, leap ? -month : month, day).getSolar();
            solar = LocalDate.of(value.getYear(), value.getMonth(), value.getDay());
        } catch (IllegalArgumentException | DateTimeException e) {
            throw invalid();
        }
        checkRange(solar);
        if (!lunarDate(solar).equals(new LunarDate(year, month, day, leap))) {
            throw invalid();
        }
        return solar;
    }

    public static LunarDate lunarDate(LocalDate solar) {
        checkRange(solar);
        try {
            Lunar value =
                    Solar.fromYmd(solar.getYear(), solar.getMonthValue(), solar.getDayOfMonth())
                            .getLunar();
            int month = value.getMonth();
            return new LunarDate(value.getYear(), Math.abs(month), value.getDay(), month < 0);
        } catch (IllegalArgumentException e) {
            // Third-party errors may contain submitted dates; expose a stable redacted error
            // instead.
            throw invalid();
        }
    }

    /** 锁定原生日历库给出的农历月天数，不叠加人工月份修补。 */
    static int monthDays(LocalDate solar) {
        var date = lunarDate(solar);
        return LunarMonth.fromYm(date.year(), date.leap() ? -date.month() : date.month())
                .getDayCount();
    }

    private static void checkRange(LocalDate solar) {
        if (solar == null || solar.isBefore(FIRST) || solar.isAfter(LAST)) {
            throw invalid();
        }
    }

    private static ZiweiDates.InputError invalid() {
        return error("ZIWEI_DATE_UNSUPPORTED", "birth.date");
    }

    private static ZiweiDates.InputError error(String code, String field) {
        return new ZiweiDates.InputError(code, field);
    }
}
