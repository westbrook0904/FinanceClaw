package com.financeclaw.infrastructure.agentscope.context;

import com.financeclaw.support.PayloadJson;
import java.util.List;
import java.util.Map;

/** 工作消息的有界结构化摘要；它是背景数据，不承担权限、审批或长期用户记忆的权威职责。 */
public record WorkContext(
        List<String> goals, List<String> tools, List<String> evidence, List<String> nextSteps) {
    private static final int MAX_ITEMS = 32;
    private static final int MAX_ITEM_CHARACTERS = 8192;

    public WorkContext {
        goals = checked(goals);
        tools = checked(tools);
        evidence = checked(evidence);
        nextSteps = checked(nextSteps);
    }

    public static WorkContext empty() {
        return new WorkContext(List.of(), List.of(), List.of(), List.of());
    }

    public String json() {
        return PayloadJson.canonical(this);
    }

    public static WorkContext fromJson(String json) {
        Map<String, Object> value = PayloadJson.object(json);
        if (!value.keySet().equals(java.util.Set.of("goals", "tools", "evidence", "nextSteps"))) {
            throw new IllegalArgumentException("Invalid work context fields");
        }
        return new WorkContext(
                strings(value.get("goals")),
                strings(value.get("tools")),
                strings(value.get("evidence")),
                strings(value.get("nextSteps")));
    }

    private static List<String> strings(Object value) {
        if (!(value instanceof List<?> items)
                || items.stream().anyMatch(item -> !(item instanceof String))) {
            throw new IllegalArgumentException("Work context fields must be string arrays");
        }
        return items.stream().map(String.class::cast).toList();
    }

    private static List<String> checked(List<String> items) {
        if (items == null
                || items.size() > MAX_ITEMS
                || items.stream()
                        .anyMatch(
                                item ->
                                        item == null
                                                || item.isBlank()
                                                || item.length() > MAX_ITEM_CHARACTERS)) {
            throw new IllegalArgumentException("Invalid work context item");
        }
        return List.copyOf(items);
    }
}
