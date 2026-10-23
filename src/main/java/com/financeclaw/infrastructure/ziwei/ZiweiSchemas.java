package com.financeclaw.infrastructure.ziwei;

import static com.financeclaw.ziwei.ZiweiValues.map;

import com.financeclaw.ziwei.ZiweiDates;
import java.util.*;

/** Public native tool schema; resource identity is checked against the fixed release contract. */
public final class ZiweiSchemas {
    private ZiweiSchemas() {}

    public static String schemaHash() {
        return ZiweiSchemaResource.hash();
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
        var properties = new TreeMap<>(map(ZiweiSchemaResource.document().get("common")));
        properties.putAll(map(map(ZiweiSchemaResource.document().get("targets")).get(tool)));
        return Map.of(
                "type",
                "object",
                "$defs",
                ZiweiSchemaResource.document().get("definitions"),
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
}
