package com.financeclaw.infrastructure.agentscope.capability;

import com.financeclaw.support.ToolSchemas;
import java.util.*;

public final class MarketToolSchemas {
  private MarketToolSchemas() {}

  public static final Map<String, Object> SYMBOL =
      Map.of("type", "string", "minLength", 1, "maxLength", 16, "pattern", "^[A-Za-z0-9._-]+$");
  public static final Map<String, Object> SNAPSHOT = ToolSchemas.required("symbol", SYMBOL);
  public static final Map<String, Object> FIELDS =
      Map.of(
          "symbols", Map.of("type", "array", "maxItems", 10, "uniqueItems", true, "items", SYMBOL),
          "analysis_period", Map.of("type", "string", "minLength", 1, "maxLength", 128));
  public static final Map<String, Object> ANSWER =
      Map.of(
          "type",
          "object",
          "properties",
          FIELDS,
          "required",
          List.of("symbols", "analysis_period"),
          "additionalProperties",
          false);
  public static final Map<String, Object> RESEARCH;

  static {
    var p = new TreeMap<>(FIELDS);
    p.put("task", Map.of("type", "string", "minLength", 1, "maxLength", 4000));
    RESEARCH =
        Map.of(
            "type",
            "object",
            "properties",
            p,
            "required",
            List.of("task"),
            "additionalProperties",
            false);
  }
}
