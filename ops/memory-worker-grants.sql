-- Run as the database administrator after Flyway. Provision the LOGIN and password separately.
-- Example: CREATE ROLE financeclaw_memory LOGIN; then use psql \password financeclaw_memory.
GRANT CONNECT ON DATABASE financeclaw_java TO financeclaw_memory;
GRANT USAGE ON SCHEMA memory TO financeclaw_memory;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA memory TO financeclaw_memory;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA memory TO financeclaw_memory;
REVOKE INSERT, UPDATE, DELETE ON memory.privacy_ledger_binding, memory.forget_requests, memory.forget_applied FROM financeclaw_memory;
REVOKE ALL ON memory.user_profiles, memory.turn_snapshots FROM financeclaw_memory;
-- No business/runtime/channel schema, no DDL, no credentials table or tool execution grants.
