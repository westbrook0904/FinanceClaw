package com.financeclaw.ziwei;

import static com.financeclaw.ziwei.ZiweiRules.*;

import com.nlf.calendar.Solar;
import java.time.LocalDate;
import java.util.*;
import java.util.stream.IntStream;

/**
 * Pure Java port of the pinned x-iztro 0.4.0 default convention. No clock, identity, tool protocol,
 * or mutable per-user state. Results expose the fixed chart JSON shape; product governance is
 * separate.
 */
public final class ZiweiChartEngine {
    public static final String ENGINE = "financeclaw-java/x-iztro-fixed-0.4.0/1";

    private ZiweiChartEngine() {}

    public enum Sex {
        MALE,
        FEMALE
    }

    public record Birth(LocalDate solarDate, int timeIndex, Sex sex) {
        public Birth {
            ZiweiCalendar.lunarDate(solarDate);
            if (timeIndex < 0 || timeIndex > 12) {
                throw new ZiweiDates.InputError("ZIWEI_TIME_AMBIGUOUS", "birth.time");
            }
            if (sex == null) {
                throw new ZiweiDates.InputError("ZIWEI_INPUT_INCOMPLETE", "birth.sex_for_chart");
            }
        }

        public static Birth of(LocalDate date, String shichen, String sex) {
            int index = ZiweiDates.SHICHEN.indexOf(shichen == null ? "" : shichen);
            Sex value =
                    switch (sex == null ? "" : sex) {
                        case "male" -> Sex.MALE;
                        case "female" -> Sex.FEMALE;
                        default ->
                                throw new ZiweiDates.InputError(
                                        "ZIWEI_TOOL_INPUT_INVALID", "birth.sex_for_chart");
                    };
            return new Birth(date, index, value);
        }
    }

    private record Dates(
            ZiweiCalendar.LunarDate lunar,
            int yearStem,
            int yearBranch,
            int monthStem,
            int monthBranch,
            int dayStem,
            int dayBranch,
            int hourStem,
            int hourBranch,
            String sign) {}

    private record Context(
            Birth birth,
            Dates dates,
            int monthIndex,
            int soul,
            int body,
            int element,
            boolean forward,
            List<Period> periods) {}

    private record Period(
            int index, int stem, int branch, int startAge, int endAge, List<Integer> ages) {}

    private static Dates dates(LocalDate date, int time) {
        var lunar = ZiweiCalendar.lunarDate(date);
        int hour = time == 0 ? 0 : time == 12 ? 23 : time * 2 - 1;
        var solar =
                Solar.fromYmdHms(
                        date.getYear(), date.getMonthValue(), date.getDayOfMonth(), hour, 0, 0);
        var raw = solar.getLunar();
        int yearStem = stem(lunar.year() - 4), yearBranch = fix(lunar.year() - 4);
        int month = lunar.month() - 1 + (lunar.leap() && lunar.day() > 15 ? 1 : 0);
        // Only day/hour cycles and zodiac sign use the native object. Lunar month data stays in our
        // adapter.
        return new Dates(
                lunar,
                yearStem,
                yearBranch,
                stem(tiger(yearStem) + month),
                fix(2 + month),
                raw.getDayGanIndexExact(),
                raw.getDayZhiIndexExact(),
                raw.getTimeGanIndex(),
                raw.getTimeZhiIndex(),
                solar.getXingZuo());
    }

    private static Context context(Birth birth) {
        if (birth == null) {
            throw new ZiweiDates.InputError("ZIWEI_INPUT_INCOMPLETE", "birth");
        }
        var dates = dates(birth.solarDate(), birth.timeIndex());
        var lunar = dates.lunar();
        int month =
                fix(
                        lunar.month()
                                - 1
                                + (lunar.leap() && lunar.day() > 15 && birth.timeIndex() != 12
                                        ? 1
                                        : 0));
        int soul = fix(month - birth.timeIndex()), body = fix(month + birth.timeIndex());
        int soulStem = stem(tiger(dates.yearStem()) + soul), soulBranch = fix(2 + soul);
        int sum = (soulStem / 2 + 1 + soulBranch % 6 / 2 + 1 - 1) % 5;
        int element = List.of(3, 4, 2, 6, 5).get(sum);
        boolean forward = (birth.sex() == Sex.MALE) == (dates.yearBranch() % 2 == 0);
        var periods = new ArrayList<Period>();
        for (int i = 0; i < 12; i++) {
            int order = fix(forward ? i - soul : soul - i);
            int start = element + 10 * order;
            int ageIndex =
                    fix(
                            birth.sex() == Sex.MALE
                                    ? i - ZiweiStars.ageStart(dates.yearBranch())
                                    : ZiweiStars.ageStart(dates.yearBranch()) - i);
            List<Integer> ages =
                    IntStream.range(0, 10).map(j -> 12 * j + ageIndex + 1).boxed().toList();
            periods.add(
                    new Period(
                            i,
                            stem(tiger(dates.yearStem()) + i),
                            fix(2 + i),
                            start,
                            start + 9,
                            ages));
        }
        return new Context(birth, dates, month, soul, body, element, forward, List.copyOf(periods));
    }

    public static Map<String, Object> natal(Birth birth) {
        var c = context(birth);
        var d = c.dates();
        var lunar = d.lunar();
        var stars =
                ZiweiStars.natal(
                        d.yearStem(),
                        d.yearBranch(),
                        c.monthIndex(),
                        lunar.day(),
                        ZiweiCalendar.monthDays(birth.solarDate()),
                        birth.timeIndex(),
                        c.soul(),
                        c.body(),
                        c.element(),
                        c.forward());
        var palaces = new ArrayList<Map<String, Object>>();
        for (var period : c.periods()) {
            int i = period.index(), nameIndex = fix(i - c.soul());
            var decadal = object("range", List.of(period.startAge(), period.endAge()));
            pillar(decadal, period.stem(), period.branch());
            var palace =
                    object(
                            "index",
                            i,
                            "name",
                            PALACES.get(nameIndex),
                            "nameKey",
                            PALACE_KEYS.get(nameIndex),
                            "isBodyPalace",
                            i == c.body(),
                            "isOriginalPalace",
                            period.branch() > 1 && period.stem() == d.yearStem(),
                            "majorStars",
                            stars.major().get(i),
                            "minorStars",
                            stars.minor().get(i),
                            "adjectiveStars",
                            stars.adjective().get(i),
                            "decadal",
                            decadal,
                            "ages",
                            period.ages(),
                            "mutagenStarKeys",
                            mutagens(period.stem()));
            pillar(palace, period.stem(), period.branch());
            translated(palace, "changsheng12", stars.changsheng().get(i));
            translated(palace, "boshi12", stars.boshi().get(i));
            translated(palace, "suiqian12", stars.suiqian().get(i));
            translated(palace, "jiangqian12", stars.jiangqian().get(i));
            palaces.add(palace);
        }
        var chinese = object();
        StringBuilder chineseText = new StringBuilder();
        addChinesePillar(chinese, chineseText, "yearly", d.yearStem(), d.yearBranch());
        addChinesePillar(chinese, chineseText, "monthly", d.monthStem(), d.monthBranch());
        addChinesePillar(chinese, chineseText, "daily", d.dayStem(), d.dayBranch());
        addChinesePillar(chinese, chineseText, "hourly", d.hourStem(), d.hourBranch());
        var rawDates =
                object(
                        "lunarDate",
                        object(
                                "lunarYear",
                                lunar.year(),
                                "lunarMonth",
                                lunar.month(),
                                "lunarDay",
                                lunar.day(),
                                "isLeap",
                                lunar.leap()),
                        "chineseDate",
                        chinese);
        int soulBranch = fix(c.soul() + 2),
                bodyBranch = fix(c.body() + 2),
                sign = SIGNS.indexOf(d.sign());
        var result =
                object(
                        "gender",
                        birth.sex() == Sex.MALE ? "男" : "女",
                        "genderKey",
                        birth.sex().name().toLowerCase(Locale.ROOT),
                        "solarDate",
                        birth.solarDate().toString(),
                        "lunarDate",
                        lunarText(lunar),
                        "chineseDate",
                        chineseText.toString(),
                        "rawDates",
                        rawDates,
                        "time",
                        TIMES.get(birth.timeIndex()),
                        "timeRange",
                        TIME_RANGES.get(birth.timeIndex()),
                        "sign",
                        d.sign() + "座",
                        "signKey",
                        SIGN_KEYS.get(sign),
                        "zodiac",
                        ZODIAC.get(d.yearBranch()),
                        "zodiacKey",
                        ZODIAC_KEYS.get(d.yearBranch()),
                        "earthlyBranchOfSoulPalace",
                        BRANCHES.get(soulBranch),
                        "earthlyBranchOfSoulPalaceKey",
                        BRANCH_KEYS.get(soulBranch),
                        "earthlyBranchOfBodyPalace",
                        BRANCHES.get(bodyBranch),
                        "earthlyBranchOfBodyPalaceKey",
                        BRANCH_KEYS.get(bodyBranch),
                        "fiveElementsClass",
                        ELEMENTS.get(c.element() - 2),
                        "fiveElementsClassKey",
                        ELEMENT_KEYS.get(c.element() - 2),
                        "palaces",
                        palaces,
                        "timeIndex",
                        birth.timeIndex(),
                        "fixLeap",
                        true,
                        "language",
                        "zh-CN",
                        "config",
                        CONFIG);
        translated(result, "soul", ZiweiTables.SOULS.get(soulBranch));
        translated(result, "body", ZiweiTables.BODIES.get(d.yearBranch()));
        return immutable(result);
    }

    /** Daily target labels always use early Zi, as defined by the five chart tool contracts. */
    public static Map<String, Object> horoscope(Birth birth, LocalDate target) {
        var c = context(birth);
        if (target == null || target.getYear() < 1901 || target.getYear() > 2099) {
            throw new ZiweiDates.InputError("ZIWEI_DATE_UNSUPPORTED", "target");
        }
        var d = dates(target, 0);
        int age = Math.max(1, d.lunar().year() - c.dates().lunar().year() + 1);
        Period decadalPeriod =
                c.periods().stream()
                        .filter(p -> age >= p.startAge() && age <= p.endAge())
                        .findFirst()
                        .orElse(null);
        boolean childhood = decadalPeriod == null;
        if (childhood) {
            int palace = List.of(0, 8, 7, 10, 2, 4).get((age - 1) % 6);
            decadalPeriod = c.periods().get(fix(c.soul() + palace));
        }
        var decadal =
                flowItem(
                        decadalPeriod.index(),
                        childhood ? "childhood" : "decadal",
                        childhood ? "童限" : "大限",
                        decadalPeriod.stem(),
                        decadalPeriod.branch(),
                        "decadal");
        var agePeriod =
                c.periods().stream()
                        .filter(p -> p.ages().contains(age))
                        .findFirst()
                        .orElse(c.periods().getFirst());
        var ageItem =
                flowItem(
                        agePeriod.index(),
                        "turn",
                        "小限",
                        agePeriod.stem(),
                        agePeriod.branch(),
                        null);
        ageItem.put("nominalAge", age);
        int yearlyIndex = branchPalace(d.yearBranch());
        var yearly = flowItem(yearlyIndex, "yearly", "流年", d.yearStem(), d.yearBranch(), "yearly");
        var sui = ZiweiStars.suiqian(d.yearBranch());
        var jiang = ZiweiStars.jiangqian(d.yearBranch());
        yearly.put(
                "yearlyDecStar",
                object(
                        "suiqian12",
                        names(sui),
                        "suiqian12Keys",
                        sui,
                        "jiangqian12",
                        names(jiang),
                        "jiangqian12Keys",
                        jiang));
        var birthLunar = c.dates().lunar();
        int birthMonth = birthLunar.month() + (birthLunar.leap() && birthLunar.day() > 15 ? 1 : 0);
        int targetMonth = d.lunar().month() + (d.lunar().leap() && d.lunar().day() > 15 ? 1 : 0);
        int monthlyIndex = fix(yearlyIndex - birthMonth + birth.timeIndex() % 12 + targetMonth);
        var monthly =
                flowItem(monthlyIndex, "monthly", "流月", d.monthStem(), d.monthBranch(), "monthly");
        int dailyIndex = fix(monthlyIndex + d.lunar().day() - 1);
        var daily = flowItem(dailyIndex, "daily", "流日", d.dayStem(), d.dayBranch(), "daily");
        var hourly = flowItem(dailyIndex, "hourly", "流时", d.hourStem(), d.hourBranch(), "hourly");
        return immutable(
                object(
                        "solarDate",
                        target.toString(),
                        "lunarDate",
                        lunarText(d.lunar()),
                        "decadal",
                        decadal,
                        "age",
                        ageItem,
                        "yearly",
                        yearly,
                        "monthly",
                        monthly,
                        "daily",
                        daily,
                        "hourly",
                        hourly));
    }

    private static Map<String, Object> flowItem(
            int index, String nameKey, String name, int stem, int branch, String scope) {
        var result =
                object(
                        "index",
                        index,
                        "name",
                        name,
                        "nameKey",
                        nameKey,
                        "palaceNames",
                        palaceNames(index, false),
                        "palaceNameKeys",
                        palaceNames(index, true),
                        "mutagen",
                        names(mutagens(stem)),
                        "mutagenStarKeys",
                        mutagens(stem));
        pillar(result, stem, branch);
        if (scope != null) {
            result.put("stars", ZiweiStars.flow(stem, branch, scope));
        }
        return result;
    }

    private static void translated(Map<String, Object> result, String field, String key) {
        result.put(field, name(key));
        result.put(field + "Key", key);
    }

    private static void addChinesePillar(
            Map<String, Object> result, StringBuilder text, String field, int stem, int branch) {
        result.put(field, List.of(STEMS.get(stem), BRANCHES.get(branch)));
        result.put(field + "Keys", List.of(STEM_KEYS.get(stem), BRANCH_KEYS.get(branch)));
        if (!text.isEmpty()) {
            text.append(' ');
        }
        text.append(STEMS.get(stem)).append(BRANCHES.get(branch));
    }
}
