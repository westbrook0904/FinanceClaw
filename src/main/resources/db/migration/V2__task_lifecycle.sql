CREATE SCHEMA business;
CREATE TABLE business.conversations (
  id text PRIMARY KEY, tenant text NOT NULL, subject text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE business.turns (
  id text PRIMARY KEY, conversation_id text NOT NULL REFERENCES business.conversations(id),
  tenant text NOT NULL, subject text NOT NULL, idempotency_key text NOT NULL,
  request_hash text NOT NULL, message text NOT NULL, release_hash text NOT NULL,
  status text NOT NULL CHECK (status IN ('queued','running','waiting','cancelling','blocked','completed','failed','cancelled')),
  revision bigint NOT NULL DEFAULT 1, result text, reason text,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE(conversation_id,idempotency_key)
);
CREATE UNIQUE INDEX one_active_turn ON business.turns(conversation_id)
 WHERE status IN ('queued','running','waiting','cancelling','blocked');
CREATE TABLE business.commands (
  id text PRIMARY KEY, turn_id text NOT NULL UNIQUE REFERENCES business.turns(id),
  status text NOT NULL CHECK (status IN ('queued','running','done')),
  attempt_id text, fence bigint NOT NULL DEFAULT 0, lease_until timestamptz,
  worker_id text, created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE business.attempts (
  id text PRIMARY KEY, command_id text NOT NULL REFERENCES business.commands(id),
  fence bigint NOT NULL, state_version bigint, outcome text, result text,
  UNIQUE(command_id,fence)
);
CREATE TABLE business.journal (
  sequence bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  conversation_id text NOT NULL REFERENCES business.conversations(id),
  turn_id text NOT NULL REFERENCES business.turns(id), role text NOT NULL, content text NOT NULL,
  UNIQUE(turn_id,role)
);
CREATE TABLE business.events (
  turn_id text NOT NULL REFERENCES business.turns(id), revision bigint NOT NULL,
  status text NOT NULL, reason text, PRIMARY KEY(turn_id,revision)
);
CREATE TABLE business.cancel_requests (
  turn_id text NOT NULL REFERENCES business.turns(id), idempotency_key text NOT NULL,
  status text NOT NULL, revision bigint NOT NULL, PRIMARY KEY(turn_id,idempotency_key)
);
CREATE TABLE business.outbox (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  turn_id text NOT NULL UNIQUE REFERENCES business.turns(id),
  revision bigint NOT NULL, status text NOT NULL, delivered boolean NOT NULL DEFAULT false
);
-- M1 local integration projection. No network delivery is performed.
CREATE TABLE business.projections (
  turn_id text PRIMARY KEY REFERENCES business.turns(id), revision bigint NOT NULL, status text NOT NULL
);
CREATE INDEX command_queue ON business.commands(created_at) WHERE status='queued';
CREATE INDEX command_expiry ON business.commands(lease_until) WHERE status='running';
