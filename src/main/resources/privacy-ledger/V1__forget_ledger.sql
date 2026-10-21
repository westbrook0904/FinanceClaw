-- Run once, using a migration administrator, in a database independent of the business database.
-- This script is deliberately outside db/migration: business Flyway must never create this ledger.
CREATE SCHEMA privacy_ledger;

CREATE TABLE privacy_ledger.lineages (
    lineage UUID PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE privacy_ledger.forget_facts (
    event_id UUID PRIMARY KEY,
    lineage UUID NOT NULL REFERENCES privacy_ledger.lineages(lineage),
    tenant TEXT NOT NULL CHECK (length(btrim(tenant)) > 0),
    subject TEXT NOT NULL CHECK (length(btrim(subject)) > 0),
    from_epoch BIGINT NOT NULL CHECK (from_epoch >= 1 AND from_epoch < 9223372036854775807),
    idempotency_key TEXT NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND 256),
    request_hash TEXT NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (lineage, tenant, subject, from_epoch),
    UNIQUE (lineage, tenant, subject, idempotency_key)
);

-- Provision a separate runtime login; never run the application as this schema's owner.
-- GRANT USAGE ON SCHEMA privacy_ledger TO your_ledger_runtime;
-- GRANT SELECT, INSERT, UPDATE ON privacy_ledger.lineages TO your_ledger_runtime;
-- GRANT SELECT, INSERT ON privacy_ledger.forget_facts TO your_ledger_runtime;
-- UPDATE on lineages is required by SELECT FOR UPDATE. No application operation updates facts.
-- Revoke database CREATE and schema CREATE from runtime roles through deployment IAM policy.
