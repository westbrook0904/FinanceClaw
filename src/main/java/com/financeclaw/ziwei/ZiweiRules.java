package com.financeclaw.ziwei;

import java.util.*;
import java.util.stream.IntStream;

/** Fixed default-rule vocabulary and JSON projection, ported from the locked MIT x-iztro source. */
final class ZiweiRules {
  private ZiweiRules() {}

  static final List<String> STEMS = words("甲 乙 丙 丁 戊 己 庚 辛 壬 癸");
  static final List<String> STEM_KEYS =
      words(
          "jiaHeavenly yiHeavenly bingHeavenly dingHeavenly wuHeavenly jiHeavenly gengHeavenly xinHeavenly renHeavenly guiHeavenly");
  static final List<String> BRANCHES = words("子 丑 寅 卯 辰 巳 午 未 申 酉 戌 亥");
  static final List<String> BRANCH_KEYS =
      words(
          "ziEarthly chouEarthly yinEarthly maoEarthly chenEarthly siEarthly wuEarthly weiEarthly shenEarthly youEarthly xuEarthly haiEarthly");
  static final List<String> PALACES = words("命宫 父母 福德 田宅 官禄 仆役 迁移 疾厄 财帛 子女 夫妻 兄弟");
  static final List<String> PALACE_KEYS =
      words(
          "soulPalace parentsPalace spiritPalace propertyPalace careerPalace friendsPalace surfacePalace healthPalace wealthPalace childrenPalace spousePalace siblingsPalace");
  static final List<String> TIMES = words("早子时 丑时 寅时 卯时 辰时 巳时 午时 未时 申时 酉时 戌时 亥时 晚子时");
  static final List<String> TIME_RANGES =
      words(
          "00:00~01:00 01:00~03:00 03:00~05:00 05:00~07:00 07:00~09:00 09:00~11:00 11:00~13:00 13:00~15:00 15:00~17:00 17:00~19:00 19:00~21:00 21:00~23:00 23:00~00:00");
  static final List<String> ZODIAC = words("鼠 牛 虎 兔 龙 蛇 马 羊 猴 鸡 狗 猪");
  static final List<String> ZODIAC_KEYS =
      words("rat ox tiger rabbit dragon snake horse sheep monkey rooster dog pig");
  static final List<String> SIGNS = words("白羊 金牛 双子 巨蟹 狮子 处女 天秤 天蝎 射手 摩羯 水瓶 双鱼");
  static final List<String> SIGN_KEYS =
      words(
          "aries taurus gemini cancer leo virgo libra scorpio sagittarius capricorn aquarius pisces");
  static final List<String> ELEMENTS = words("水二局 木三局 金四局 土五局 火六局");
  static final List<String> ELEMENT_KEYS = words("water2nd wood3rd metal4th earth5th fire6th");
  static final List<String> MUTAGEN_NAMES = words("禄 权 科 忌");
  static final List<String> MUTAGEN_KEYS = words("sihuaLu sihuaQuan sihuaKe sihuaJi");
  static final Map<String, String> BRIGHTNESS_NAMES =
      Map.of("miao", "庙", "wang", "旺", "de", "得", "li", "利", "ping", "平", "bu", "不", "xian", "陷");
  static final Map<String, Object> CONFIG =
      Map.of(
          "yearDivide",
          "normal",
          "horoscopeDivide",
          "normal",
          "ageDivide",
          "normal",
          "dayDivide",
          "forward",
          "algorithm",
          "default",
          "astroType",
          "heaven");

  static List<String> words(String value) {
    return List.of(value.split(" "));
  }

  static int fix(int value) {
    return Math.floorMod(value, 12);
  }

  static int stem(int value) {
    return Math.floorMod(value, 10);
  }

  static int branchPalace(int branch) {
    return fix(branch - 2);
  }

  static int tiger(int yearStem) {
    return stem((yearStem % 5) * 2 + 2);
  }

  static Map<String, Object> object(Object... pairs) {
    var result = new LinkedHashMap<String, Object>();
    for (int i = 0; i < pairs.length; i += 2) result.put((String) pairs[i], pairs[i + 1]);
    return result;
  }

  static void pillar(Map<String, Object> object, int stem, int branch) {
    object.put("heavenlyStem", STEMS.get(stem));
    object.put("heavenlyStemKey", STEM_KEYS.get(stem));
    object.put("earthlyBranch", BRANCHES.get(branch));
    object.put("earthlyBranchKey", BRANCH_KEYS.get(branch));
  }

  static List<String> palaceNames(int soul, boolean keys) {
    var source = keys ? PALACE_KEYS : PALACES;
    return IntStream.range(0, 12).mapToObj(i -> source.get(fix(i - soul))).toList();
  }

  static String name(String key) {
    return Objects.requireNonNull(ZiweiTables.NAMES.get(key));
  }

  static List<String> names(List<String> keys) {
    return keys.stream().map(ZiweiRules::name).toList();
  }

  static List<String> mutagens(int stem) {
    return ZiweiTables.MUTAGENS.get(stem);
  }

  static String lunarText(ZiweiCalendar.LunarDate lunar) {
    String digits = "〇一二三四五六七八九";
    StringBuilder year = new StringBuilder();
    for (char c : Integer.toString(lunar.year()).toCharArray()) year.append(digits.charAt(c - '0'));
    var months = words("正 二 三 四 五 六 七 八 九 十 冬 腊");
    var days =
        words(
            "初一 初二 初三 初四 初五 初六 初七 初八 初九 初十 十一 十二 十三 十四 十五 十六 十七 十八 十九 二十 廿一 廿二 廿三 廿四 廿五 廿六 廿七 廿八 廿九 三十");
    return year
        + "年"
        + (lunar.leap() ? "闰" : "")
        + months.get(lunar.month() - 1)
        + "月"
        + days.get(lunar.day() - 1);
  }

  /** Freeze the complete result graph so callers cannot change cached or concurrent results. */
  static Map<String, Object> immutable(Map<String, Object> value) {
    var copy = new LinkedHashMap<String, Object>();
    value.forEach((key, item) -> copy.put(key, freeze(item)));
    return Collections.unmodifiableMap(copy);
  }

  private static Object freeze(Object value) {
    if (value instanceof Map<?, ?> map) {
      var copy = new LinkedHashMap<String, Object>();
      map.forEach((key, item) -> copy.put((String) key, freeze(item)));
      return Collections.unmodifiableMap(copy);
    }
    if (value instanceof List<?> list) return list.stream().map(ZiweiRules::freeze).toList();
    return value;
  }
}
