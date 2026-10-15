-- Owner-scoped admission control is durable business state, separate from worker execution.
CREATE TABLE business.admission (
  tenant text NOT NULL, subject text NOT NULL, paused boolean NOT NULL DEFAULT false,
  revision bigint NOT NULL DEFAULT 0 CHECK(revision >= 0),
  PRIMARY KEY(tenant,subject)
);
CREATE TABLE business.admission_changes (
  tenant text NOT NULL, subject text NOT NULL, idempotency_key text NOT NULL,
  request_hash text NOT NULL, paused boolean NOT NULL, revision bigint NOT NULL CHECK(revision > 0),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY(tenant,subject,idempotency_key), UNIQUE(tenant,subject,revision),
  FOREIGN KEY(tenant,subject) REFERENCES business.admission(tenant,subject)
);
CREATE INDEX turn_owner_status ON business.turns(tenant,subject,status);
