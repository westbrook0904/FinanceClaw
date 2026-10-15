-- Run after Flyway, on PostgreSQL with pgvector 0.8.1 installed, using a DDL operator.
-- Facts remain authoritative; this index and the projection table may be rebuilt.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE INDEX IF NOT EXISTS memory_projection_cosine ON memory.projections
  USING hnsw ((embedding::vector(1024)) vector_cosine_ops);
