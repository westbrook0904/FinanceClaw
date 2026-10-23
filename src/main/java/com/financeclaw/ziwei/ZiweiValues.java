package com.financeclaw.ziwei;

import com.financeclaw.support.PayloadJson;
import java.nio.charset.StandardCharsets;
import java.util.*;

/** Deeply immutable JSON-shaped values, including nullable artifact fields. */
public final class ZiweiValues {
    private ZiweiValues() {}

    @SuppressWarnings("unchecked")
    public static Map<String, Object> immutable(Map<String, Object> map) {
        return (Map<String, Object>) freeze(map);
    }

    private static Object freeze(Object value) {
        if (value instanceof Map<?, ?> m) {
            var out = new TreeMap<String, Object>();
            m.forEach((k, v) -> out.put((String) k, freeze(v)));
            return Collections.unmodifiableMap(out);
        }
        if (value instanceof List<?> l) {
            return l.stream().map(ZiweiValues::freeze).toList();
        }
        return value;
    }

    @SuppressWarnings("unchecked")
    public static Map<String, Object> map(Object value) {
        return value instanceof Map<?, ?> ? (Map<String, Object>) value : Map.of();
    }

    public static int bytes(Object value) {
        return PayloadJson.canonical(value).getBytes(StandardCharsets.UTF_8).length;
    }
}
