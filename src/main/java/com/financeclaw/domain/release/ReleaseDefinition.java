package com.financeclaw.domain.release;

import java.util.*;

/** Immutable release values only: no resource or secret loading and no serialization policy. */
public record ReleaseDefinition(
    String version,
    String agent,
    List<String> scopes,
    ModelProfile primary,
    ModelProfile fallback,
    int maxModelCalls,
    int maxToolCalls,
    BusinessFeatures business) {

  public ReleaseDefinition {
    scopes = List.copyOf(scopes);
  }

  public static final List<String> SCOPES =
      List.of(
          "catalog:read",
          "report:write",
          "artifact:read",
          "travel:read",
          "taibu:read",
          "skill:use",
          "market:read",
          "research:run",
          "workflow:run",
          "ziwei:read");
  public static final ModelProfile OFFLINE =
      new ModelProfile("offline", "", 65536, 131072, 2048, 512, false);

  public void validate() {
    if (!"finance_agent".equals(agent)
        || !SCOPES.containsAll(scopes)
        || maxModelCalls < 1
        || maxToolCalls < 1) throw new IllegalArgumentException("Invalid release policy");
    primary.validate();
    if (business.ziwei() != null) business.ziwei().verifyRuntime();
    if (fallback != null) fallback.validate();
  }
}
