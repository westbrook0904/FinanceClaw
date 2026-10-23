-- Isolated M0 experiment fixture. This probe table is not part of the application schema.
CREATE TABLE agent_runtime.tool_effects (
    scope_key VARCHAR(64) NOT NULL,
    operation_id VARCHAR(128) NOT NULL,
    request_hash VARCHAR(64) NOT NULL,
    status VARCHAR(16) NOT NULL CHECK (status IN ('pending', 'completed')),
    result_json JSONB,
    PRIMARY KEY (scope_key, operation_id),
    CHECK ((status = 'completed') = (result_json IS NOT NULL))
);
