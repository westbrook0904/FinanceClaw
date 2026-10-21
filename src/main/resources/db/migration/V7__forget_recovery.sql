-- This binding survives ordinary database backups. The authoritative forget ledger is separate.
CREATE TABLE memory.privacy_ledger_binding (
  singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
  lineage text NOT NULL DEFAULT gen_random_uuid()::text,
  active boolean NOT NULL DEFAULT false
);
INSERT INTO memory.privacy_ledger_binding(singleton) VALUES(true);

-- Commit the request and the owner barrier before contacting the independent ledger.
CREATE TABLE memory.forget_requests (
  tenant text NOT NULL, subject text NOT NULL, event_id text NOT NULL UNIQUE,
  lineage text NOT NULL, from_epoch bigint NOT NULL CHECK(from_epoch >= 1 AND from_epoch < 9223372036854775807),
  operation_key text NOT NULL, request_hash text NOT NULL,
  PRIMARY KEY(tenant,subject),
  FOREIGN KEY(tenant,subject) REFERENCES memory.owners(tenant,subject)
);

-- Retain stable event identities and operation receipts when replaying old backups.
CREATE TABLE memory.forget_applied (
  event_id text PRIMARY KEY, lineage text NOT NULL, tenant text NOT NULL, subject text NOT NULL,
  from_epoch bigint NOT NULL CHECK(from_epoch >= 1 AND from_epoch < 9223372036854775807),
  operation_key text NOT NULL, request_hash text NOT NULL,
  UNIQUE(tenant,subject,from_epoch),
  FOREIGN KEY(tenant,subject) REFERENCES memory.owners(tenant,subject)
);
