-- Roles are provisioned by the operator before this one-shot schema initialization.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA agent_runtime, business, memory, channel TO financeclaw_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA agent_runtime, business, memory, channel TO financeclaw_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA agent_runtime, business, memory, channel TO financeclaw_app;
GRANT USAGE ON SCHEMA memory TO financeclaw_memory;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA memory TO financeclaw_memory;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA memory TO financeclaw_memory;
REVOKE INSERT, UPDATE, DELETE ON memory.privacy_ledger_binding, memory.forget_requests, memory.forget_applied FROM financeclaw_memory;
-- Human-maintained profiles are read and changed only by authenticated API/isolated recovery.
REVOKE ALL ON memory.user_profiles FROM financeclaw_memory;
CREATE INDEX IF NOT EXISTS memory_projection_cosine ON memory.projections
  USING hnsw ((embedding::vector(1024)) vector_cosine_ops);
