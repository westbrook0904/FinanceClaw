package com.financeclaw.domain.memory;

import java.util.*;

/** Validated finite 1024-dimensional values with the original float conversion contract. */
public final class EmbeddingVector {
  private final List<Float> values;

  private EmbeddingVector(List<Float> values) {
    this.values = List.copyOf(values);
  }

  public List<Float> values() {
    return values;
  }

  public static EmbeddingVector of(double[] vector) {
    if (vector == null || vector.length != 1024)
      throw new IllegalArgumentException("embedding_dimension_mismatch");
    double norm = 0;
    var parts = new ArrayList<Float>(1024);
    for (double v : vector) {
      if (!Double.isFinite(v) || Math.abs(v) > Float.MAX_VALUE)
        throw new IllegalArgumentException("invalid_embedding");
      norm += v * v;
      parts.add((float) v);
    }
    if (norm == 0) throw new IllegalArgumentException("zero_embedding");
    return new EmbeddingVector(parts);
  }
}
