package com.financeclaw.domain;

import java.util.List;

/** Task-level constraints; provider credentials and SDK configuration stay outside the use case. */
public record ExecutionPolicy(List<String> scopes, int maxModelCalls, int maxToolCalls) {
  public ExecutionPolicy {
    scopes = List.copyOf(scopes);
  }
}
