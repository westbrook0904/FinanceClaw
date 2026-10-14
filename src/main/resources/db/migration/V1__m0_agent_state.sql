CREATE SCHEMA IF NOT EXISTS agent_runtime;

CREATE TABLE agent_runtime.states (
    namespace VARCHAR(64) NOT NULL,
    user_id VARCHAR(64) NOT NULL,
    session_id VARCHAR(64) NOT NULL,
    state_key VARCHAR(128) NOT NULL,
    version BIGINT NOT NULL CHECK (version > 0),
    payload JSONB,
    deleted BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (namespace, user_id, session_id, state_key),
    CHECK (deleted = (payload IS NULL))
);

-- M0 experiment ledger: a pending write is never automatically dispatched twice.
CREATE TABLE agent_runtime.tool_effects (
    scope_key VARCHAR(64) NOT NULL,
    operation_id VARCHAR(128) NOT NULL,
    request_hash VARCHAR(64) NOT NULL,
    status VARCHAR(16) NOT NULL CHECK (status IN ('pending', 'completed')),
    result_json JSONB,
    PRIMARY KEY (scope_key, operation_id),
    CHECK ((status = 'completed') = (result_json IS NOT NULL))
);
