package com.financeclaw.infrastructure.memory;

import com.financeclaw.domain.memory.EmbeddingVector;
import java.util.stream.Collectors;

/** PostgreSQL literal representation is confined to persistence. */
public final class MemoryVectorCodec {
    private MemoryVectorCodec() {}

    public static String array(EmbeddingVector vector) {
        return vector.values().stream()
                .map(String::valueOf)
                .collect(Collectors.joining(",", "{", "}"));
    }

    public static String query(EmbeddingVector vector) {
        return array(vector).replace('{', '[').replace('}', ']');
    }
}
