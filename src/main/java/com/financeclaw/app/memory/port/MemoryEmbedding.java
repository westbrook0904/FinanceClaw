package com.financeclaw.app.memory.port;

import com.financeclaw.domain.memory.EmbeddingVector;

@FunctionalInterface
public interface MemoryEmbedding {
  EmbeddingVector embed(String text);
}
