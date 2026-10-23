package com.financeclaw.domain.memory;

import com.financeclaw.domain.Task.Problem;

/** 记忆功能策略：证据提取、向量召回与人工画像独立启用；禁用功能仍保留执行所需的隐私隔离检查。 */
public record MemoryPolicy(
        boolean enabled, boolean vectorSearch, String embeddingRevision, boolean profileEnabled) {
    private static final int MAX_SEARCH_QUERY_CHARS = 1_024;

    public boolean anyEnabled() {
        return enabled || profileEnabled;
    }

    public void requireAnyEnabled() {
        if (!anyEnabled()) {
            throw new Problem(503, "memory_disabled");
        }
    }

    public void requireProfileEnabled() {
        if (!profileEnabled) {
            throw new Problem(503, "profile_disabled");
        }
    }

    public void requireEnabled() {
        if (!enabled) {
            throw new Problem(503, "memory_disabled");
        }
    }

    public void requireSearch(String query) {
        if (!vectorSearch || !enabled) {
            throw new Problem(503, "vector_search_disabled");
        }
        if (query == null || query.isBlank() || query.length() > MAX_SEARCH_QUERY_CHARS) {
            throw new Problem(422, "invalid_query");
        }
    }
}
