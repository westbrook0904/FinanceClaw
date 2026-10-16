package com.financeclaw.app.memory.port;

import com.financeclaw.domain.Memory.Extraction;

@FunctionalInterface
public interface MemoryExtraction {
  Extraction extract(String source);
}
