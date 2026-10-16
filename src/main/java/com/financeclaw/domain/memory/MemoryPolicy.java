package com.financeclaw.domain.memory;

import com.financeclaw.domain.Task.Problem;

/** Application flags and immutable embedding revision, supplied by deployment configuration. */
public record MemoryPolicy(boolean enabled, boolean vectorSearch, String embeddingRevision) {
  public void requireEnabled() {
    if (!enabled) throw new Problem(503, "memory_disabled");
  }

  public void requireSearch(String query) {
    if (!vectorSearch || !enabled) throw new Problem(503, "vector_search_disabled");
    if (query == null || query.isBlank() || query.length() > 1024)
      throw new Problem(422, "invalid_query");
  }
}
