package com.financeclaw.ziwei;

import static com.financeclaw.ziwei.ZiweiRules.*;

import java.util.*;
import java.util.stream.IntStream;

/**
 * Star placement under the single fixed default convention; MIT x-iztro source in dependency lock.
 */
final class ZiweiStars {
  private ZiweiStars() {}

  record Placement(
      List<List<Map<String, Object>>> major,
      List<List<Map<String, Object>>> minor,
      List<List<Map<String, Object>>> adjective,
      List<String> changsheng,
      List<String> boshi,
      List<String> suiqian,
      List<String> jiangqian) {}

  static List<List<Map<String, Object>>> emptyPalaces() {
    return IntStream.range(0, 12)
        .mapToObj(i -> (List<Map<String, Object>>) new ArrayList<Map<String, Object>>())
        .toList();
  }

  private static Map<String, Object> star(
      String key, String type, String scope, int index, Integer yearStem, boolean natal) {
    var result = object("key", key, "name", name(key), "type", type, "scope", scope);
    if (natal) {
      String brightness =
          ZiweiTables.BRIGHTNESS.getOrDefault(key, Collections.nCopies(12, "")).get(index);
      result.put("brightness", BRIGHTNESS_NAMES.getOrDefault(brightness, ""));
      if (!brightness.isEmpty()) result.put("brightnessKey", brightness);
    }
    if (yearStem != null) {
      int position = mutagens(yearStem).indexOf(key);
      result.put("mutagen", position < 0 ? "" : MUTAGEN_NAMES.get(position));
      if (position >= 0) result.put("mutagenKey", MUTAGEN_KEYS.get(position));
    }
    return result;
  }

  private static void place(
      List<List<Map<String, Object>>> palaces,
      int index,
      String key,
      String type,
      String scope,
      Integer yearStem,
      boolean natal) {
    int at = fix(index);
    palaces.get(at).add(star(key, type, scope, at, yearStem, natal));
  }

  private static void adjective(
      List<List<Map<String, Object>>> palaces, int index, String key, String type) {
    place(palaces, index, key, type, "origin", null, false);
  }

  static int lu(int stem) {
    return List.of(0, 1, 3, 4, 3, 4, 6, 7, 9, 10).get(stem);
  }

  private static int ma(int branch) {
    return List.of(0, 9, 6, 3).get(branch % 4);
  }

  private static int kui(int stem) {
    return List.of(11, 10, 9, 9, 11, 10, 11, 4, 1, 1).get(stem);
  }

  private static int yue(int stem) {
    return List.of(5, 6, 7, 7, 5, 6, 5, 0, 3, 3).get(stem);
  }

  static int ageStart(int branch) {
    return List.of(8, 5, 2, 11).get(branch % 4);
  }

  private static int nianjie(int branch) {
    return fix(8 - branch);
  }

  static Placement natal(
      int yearStem,
      int yearBranch,
      int monthIndex,
      int lunarDay,
      int monthDays,
      int timeIndex,
      int soul,
      int body,
      int element,
      boolean forward) {
    var major = emptyPalaces();
    var minor = emptyPalaces();
    var adj = emptyPalaces();
    int day = lunarDay + (timeIndex == 12 ? 1 : 0);
    if (day > monthDays) day -= monthDays;
    int offset = Math.floorMod(-day, element);
    int ziwei = fix((day + offset) / element - 1 + (offset % 2 == 0 ? offset : -offset));
    int tianfu = fix(-ziwei);
    var majorKeys =
        words(
            "ziweiMaj tianjiMaj taiyangMaj wuquMaj tiantongMaj lianzhenMaj tianfuMaj taiyinMaj tanlangMaj jumenMaj tianxiangMaj tianliangMaj qishaMaj pojunMaj");
    var offsets = List.of(0, 1, 3, 4, 5, 8, 0, 1, 2, 3, 4, 5, 6, 10);
    for (int i = 0; i < 14; i++)
      place(
          major,
          i < 6 ? ziwei - offsets.get(i) : tianfu + offsets.get(i),
          majorKeys.get(i),
          "major",
          "origin",
          yearStem,
          true);

    int time = timeIndex % 12;
    int zuo = fix(2 + monthIndex),
        you = fix(8 - monthIndex),
        chang = fix(8 - time),
        qu = fix(2 + time);
    int lu = lu(yearStem);
    int huo = fix(List.of(0, 1, 11, 7).get(yearBranch % 4) + time);
    int ling = fix((yearBranch % 4 == 2 ? 1 : 8) + time);
    var minorKeys =
        words(
            "zuofuMin youbiMin wenchangMin wenquMin tiankuiMin tianyueMin lucunMin tianmaMin dikongMin dijieMin huoxingMin lingxingMin qingyangMin tuoluoMin");
    var minorTypes =
        words("soft soft soft soft soft soft lucun tianma tough tough tough tough tough tough");
    var indices =
        List.of(
            zuo,
            you,
            chang,
            qu,
            kui(yearStem),
            yue(yearStem),
            lu,
            ma(yearBranch),
            fix(9 - time),
            fix(9 + time),
            huo,
            ling,
            fix(lu + 1),
            fix(lu - 1));
    for (int i = 0; i < 14; i++)
      place(
          minor,
          indices.get(i),
          minorKeys.get(i),
          minorTypes.get(i),
          "origin",
          i < 4 ? yearStem : null,
          true);

    // Preserve source placement order, including the two distinct characters 飞廉 and 蜚廉.
    int dayIndex = lunarDay - (timeIndex == 12 ? 0 : 1);
    int b = yearBranch, s = yearStem, m = monthIndex;
    int xunkong = fix(branchPalace(b) + 10 - s);
    if (b % 2 != xunkong % 2) xunkong = fix(xunkong + 1);
    var headIndices =
        List.of(
            fix(1 - b),
            fix(7 - b),
            fix(11 + m),
            List.of(7, 4, 1, 10).get(b % 4),
            List.of(6, 8, 10, 0, 2, 4).get(m / 2),
            fix(zuo + dayIndex),
            fix(you - dayIndex),
            fix(chang + dayIndex - 1),
            fix(qu + dayIndex - 1),
            fix(2 + b),
            fix(8 - b),
            fix(soul + b),
            fix(body + b),
            fix(4 + time),
            time,
            List.of(3, 6, 0, 9).get(m % 4),
            List.of(2, 11, 8, 5).get(b % 4),
            List.of(5, 2, 3, 0, 1, 7, 9, 7, 8, 4).get(s),
            List.of(7, 6, 10, 9, 1, 0, 4, 3, 4, 3).get(s),
            List.of(3, 4, 10, 3, 4, 6, 0, 4, 7, 9).get(s),
            List.of(8, 3, 2, 0, 5, 1, 9, 5, 0, 4, 8, 0).get(m),
            fix(7 + b),
            fix(3 + b),
            fix(branchPalace(b) + 1),
            xunkong);
    var headKeys =
        words(
            "hongluan tianxi tianyao xianchi jieshen santai bazuo engguang tiangui longchi fengge tiancai tianshou taifu fenggao tianwu huagai tianguan tianfu tianchu tianyue tiande yuede tiankong xunkong");
    for (int i = 0; i < headKeys.size(); i++)
      adjective(
          adj,
          headIndices.get(i),
          headKeys.get(i),
          i < 4 ? "flower" : i == 4 ? "helper" : "adjective");
    adjective(adj, List.of(6, 4, 2, 0, 10).get(s % 5), "jielu", "adjective");
    adjective(adj, List.of(7, 5, 3, 1, 11).get(s % 5), "kongwang", "adjective");
    int season = ((b + 10) % 12) / 3;
    var tailIndices =
        List.of(
            List.of(3, 6, 9, 0).get(season),
            List.of(11, 2, 5, 8).get(season),
            List.of(6, 7, 8, 3, 4, 5, 0, 1, 2, 9, 10, 11).get(b),
            List.of(3, 11, 7).get(b % 3),
            fix(7 + m),
            List.of(0, 10, 8, 6, 4, 2).get(m % 6),
            fix(4 - b),
            fix(4 + b),
            fix(7 + soul),
            fix(5 + soul),
            nianjie(b));
    var tailKeys =
        words("guchen guasu feilian posui tianxing yinsha tianku tianxu tianshi tianshang nianjie");
    for (int i = 0; i < tailKeys.size(); i++)
      adjective(adj, tailIndices.get(i), tailKeys.get(i), i == 10 ? "helper" : "adjective");

    int start = List.of(6, 9, 3, 6, 0).get(element - 2);
    return new Placement(
        major,
        minor,
        adj,
        rotate(
            words("changsheng muyu guandai linguan diwang shuai bing si mu jue tai yang"),
            start,
            forward),
        rotate(
            words(
                "boshi lishi qinglong xiaohao jiangjun zhoushu faylian xishen bingfu dahao fubing guanfu"),
            lu,
            forward),
        suiqian(b),
        jiangqian(b));
  }

  private static List<String> rotate(List<String> stars, int start, boolean forward) {
    return IntStream.range(0, 12)
        .mapToObj(i -> stars.get(fix(forward ? i - start : start - i)))
        .toList();
  }

  static List<String> suiqian(int branch) {
    return rotate(
        words(
            "suijian huiqi sangmen guansuo gwanfu xiaohao dahao longde baihu tiande diaoke bingfu"),
        branchPalace(branch),
        true);
  }

  static List<String> jiangqian(int branch) {
    return rotate(
        words(
            "jiangxing panan suiyi xiishen huagai jiesha zhaisha tiansha zhibei xianchi yuesha wangshen"),
        List.of(10, 7, 4, 1).get(branch % 4),
        true);
  }

  static List<List<Map<String, Object>>> flow(int stem, int branch, String scope) {
    var result = emptyPalaces();
    String prefix =
        switch (scope) {
          case "decadal" -> "yun";
          case "yearly" -> "liu";
          case "monthly" -> "yue";
          case "daily" -> "ri";
          case "hourly" -> "shi";
          default -> throw new IllegalArgumentException("Unknown fixed scope");
        };
    if (scope.equals("yearly"))
      place(result, nianjie(branch), "nianjie", "helper", scope, null, false);
    int lu = lu(stem);
    var indices =
        List.of(
            kui(stem),
            yue(stem),
            List.of(3, 4, 6, 7, 6, 7, 9, 10, 0, 1).get(stem),
            List.of(7, 6, 4, 3, 4, 3, 1, 0, 10, 9).get(stem),
            lu,
            fix(lu + 1),
            fix(lu - 1),
            ma(branch),
            fix(1 - branch),
            fix(7 - branch));
    var suffixes = words("kui yue chang qu lu yang tuo ma luan xi");
    var types = words("soft soft soft soft lucun tough tough tianma flower flower");
    for (int i = 0; i < 10; i++)
      place(result, indices.get(i), prefix + suffixes.get(i), types.get(i), scope, null, false);
    return result;
  }
}
