package com.financeclaw.support;

import java.util.List;
import java.util.Map;

/** Small schema literals shared by tool declarations; validation belongs to the SDK. */
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
