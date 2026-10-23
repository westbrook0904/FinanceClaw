package com.financeclaw.tools;

import com.financeclaw.domain.Governance.Blocked;
import java.time.*;
import java.util.*;

/**
 * 外部工具参数和结果的业务约束，阻止不完整或越界的领域输入进入执行链。
 *
 * <p>结构由工具 Schema 校验；这里补充字段间关联和结果语义校验，失败时只暴露稳定业务错误。
 */
public final class TaibuInputs {
    private TaibuInputs() {}

    public static Object validateResult(String tool, Object data, Map<String, Object> input) {
        try {
            if (!(data instanceof Map<?, ?> map) || map.isEmpty()) {
                throw new IllegalArgumentException();
            }
            if (tool.equals("almanac")) {
                if (!(map.get("基础与个性化坐标") instanceof Map<?, ?> coordinate)
                        || !Objects.equals(coordinate.get("日期"), input.get("date"))
                        || !(map.get("择日宜忌") instanceof Map<?, ?> activities)) {
                    throw new IllegalArgumentException();
                }
                for (String key : List.of("宜", "忌")) {
                    if (!(activities.get(key) instanceof List<?> items) || items.isEmpty()) {
                        throw new IllegalArgumentException();
                    }
                }
            } else {
                if (!(map.get("四柱") instanceof List<?> pillars) || pillars.size() != 4) {
                    throw new IllegalArgumentException();
                }
                Set<String> labels = new HashSet<>();
                for (Object p : pillars) {
                    if (!(p instanceof Map<?, ?> pillar)
                            || !(pillar.get("干支") instanceof String ganzhi)
                            || !ganzhi.matches("[甲乙丙丁戊己庚辛壬癸][子丑寅卯辰巳午未申酉戌亥]")) {
                        throw new IllegalArgumentException();
                    }
                    labels.add((String) pillar.get("柱"));
                }
                if (!labels.equals(Set.of("年柱", "月柱", "日柱", "时柱"))
                        || !(map.get("基本信息") instanceof Map<?, ?> info)
                        || !Objects.equals(
                                info.get("性别"), input.get("gender").equals("male") ? "男" : "女")) {
                    throw new IllegalArgumentException();
                }
                if (input.containsKey("longitude")
                        && (!(map.get("placeResolutionInfo") instanceof Map<?, ?> place)
                                || !Boolean.TRUE.equals(place.get("resolved"))
                                || !"manual_input".equals(place.get("source"))
                                || !Objects.equals(
                                        place.get("usedLongitude"), input.get("longitude")))) {
                    throw new IllegalArgumentException();
                }
            }
            return data;
        } catch (RuntimeException e) {
            throw new Blocked("taibu_invalid_result");
        }
    }

    /** Additional domain checks on the native MCP schema. No alias or argument remapping. */
    public static void validate(String tool, Map<String, Object> input) {
        try {
            if (tool.equals("almanac")) {
                LocalDate date = LocalDate.parse((String) input.get("date"));
                if (date.getYear() < 1900 || date.getYear() > 2100) {
                    throw new IllegalArgumentException();
                }
            } else {
                for (String key :
                        List.of(
                                "gender",
                                "calendarType",
                                "birthYear",
                                "birthMonth",
                                "birthDay",
                                "birthHour",
                                "birthMinute")) {
                    if (!input.containsKey(key)) {
                        throw new IllegalArgumentException();
                    }
                }
                if (input.get("calendarType").equals("solar")) {
                    LocalDate.of(
                            ((Number) input.get("birthYear")).intValue(),
                            ((Number) input.get("birthMonth")).intValue(),
                            ((Number) input.get("birthDay")).intValue());
                    if (Boolean.TRUE.equals(input.get("isLeapMonth"))) {
                        throw new IllegalArgumentException();
                    }
                } else if (!input.containsKey("isLeapMonth")
                        || ((Number) input.get("birthDay")).intValue() > 30) {
                    throw new IllegalArgumentException();
                }
            }
        } catch (RuntimeException e) {
            throw new Blocked("taibu_invalid_input");
        }
    }
}
