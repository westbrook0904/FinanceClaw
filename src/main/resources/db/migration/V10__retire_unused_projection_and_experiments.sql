-- Stop old business processes and archive these retired tables before applying this migration.
-- No CASCADE: unexpected external dependencies must block the upgrade instead of being erased.
-- V1-V9 remain immutable so existing databases can validate and upgrade normally.
DROP TABLE business.projections;
DROP TABLE business.outbox;
DROP TABLE business.skill_refs;
DROP TABLE agent_runtime.tool_effects;
