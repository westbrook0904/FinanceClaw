package com.financeclaw.support;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.SerializationFeature;
import com.fasterxml.jackson.databind.json.JsonMapper;
import com.financeclaw.domain.Governance.Blocked;
import java.util.List;
import java.util.Map;

/** Persisted business JSON contract; SDK AgentState continues to use its own codec. */
public final class PayloadJson {
  private static final JsonMapper WRITER =
      JsonMapper.builder().enable(SerializationFeature.ORDER_MAP_ENTRIES_BY_KEYS).build();
  private static final ObjectMapper READER = new ObjectMapper();

  private PayloadJson() {}

  public static String canonical(Object input) {
    try {
      return WRITER.writeValueAsString(input);
    } catch (JsonProcessingException e) {
      throw new IllegalArgumentException("Invalid action payload", e);
    }
  }

  public static Object parse(String json) {
    try {
      return READER.readValue(json, Object.class);
    } catch (Exception e) {
      throw new Blocked("invalid_stored_json");
    }
  }

  public static Map<String, Object> object(String json) {
    try {
      return READER.readValue(json, new TypeReference<Map<String, Object>>() {});
    } catch (Exception e) {
      throw new Blocked("invalid_stored_schema");
    }
  }

  public static List<String> scopes(String json) {
    try {
      return READER.readValue(json, new TypeReference<List<String>>() {});
    } catch (Exception e) {
      throw new Blocked("invalid_stored_scopes");
    }
  }
}
