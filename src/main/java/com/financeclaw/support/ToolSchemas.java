package com.financeclaw.support;

import java.util.List;
import java.util.Map;

/**
 * 构造工具输入的 JSON Schema，集中声明必填字段和字段类型。
 *
 * <p>这里只生成静态契约，运行参数校验和工具执行仍由 AgentScope 与对应业务边界负责。
 */
public final class ToolSchemas {
    private ToolSchemas() {}

    public static Map<String, Object> required(String key, Map<String, Object> property) {
        return Map.of(
                "type",
                "object",
                "properties",
                Map.of(key, property),
                "required",
                List.of(key),
                "additionalProperties",
                false);
    }
}
