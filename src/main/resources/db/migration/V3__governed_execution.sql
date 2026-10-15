ALTER TABLE business.events ADD COLUMN kind text NOT NULL DEFAULT 'turn';
ALTER TABLE business.events ADD COLUMN text text;
ALTER TABLE business.events ADD COLUMN tool text;
CREATE TABLE business.grants (
  conversation_id text PRIMARY KEY REFERENCES business.conversations(id),
  revision bigint NOT NULL DEFAULT 1, scopes jsonb NOT NULL
);
CREATE TABLE business.grant_changes (
  conversation_id text NOT NULL REFERENCES business.conversations(id), idempotency_key text NOT NULL,
  request_hash text NOT NULL, revision bigint NOT NULL, scopes jsonb NOT NULL,
  PRIMARY KEY(conversation_id,idempotency_key)
);
CREATE TABLE business.turn_policies (
  turn_id text PRIMARY KEY REFERENCES business.turns(id), grant_revision bigint NOT NULL,
  scopes jsonb NOT NULL, model_calls int NOT NULL DEFAULT 0, tool_calls int NOT NULL DEFAULT 0
);
CREATE TABLE business.interactions (
  id text PRIMARY KEY, turn_id text NOT NULL REFERENCES business.turns(id),
  command_id text NOT NULL REFERENCES business.commands(id), attempt_id text NOT NULL REFERENCES business.attempts(id),
  state_version bigint NOT NULL, grant_revision bigint NOT NULL, release_hash text NOT NULL,
  kind text NOT NULL CHECK(kind IN ('approval','input')), call_id text NOT NULL, tool text NOT NULL,
  action_hash text NOT NULL, schema_json text NOT NULL, question text NOT NULL, revision bigint NOT NULL DEFAULT 1,
  status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','answered','invalid')),
  expires_at timestamptz NOT NULL DEFAULT (clock_timestamp()+interval '15 minutes'),
  response_hash text, response_json text, UNIQUE(attempt_id,call_id)
);
ALTER TABLE business.commands ADD COLUMN resume_id text REFERENCES business.interactions(id);
ALTER TABLE business.attempts ADD COLUMN resume_id text REFERENCES business.interactions(id);
ALTER TABLE business.turns DROP CONSTRAINT turns_status_check;
ALTER TABLE business.turns ADD CONSTRAINT turns_status_check CHECK(status IN ('queued','resuming','running','waiting','cancelling','blocked','completed','failed','cancelled'));
DROP INDEX business.one_active_turn;
CREATE UNIQUE INDEX one_active_turn ON business.turns(conversation_id) WHERE status IN ('queued','resuming','running','waiting','cancelling','blocked');
CREATE TABLE business.artifacts (
  id text PRIMARY KEY, turn_id text NOT NULL REFERENCES business.turns(id),
  conversation_id text NOT NULL REFERENCES business.conversations(id), tenant text NOT NULL, subject text NOT NULL,
  release_hash text NOT NULL, hash text NOT NULL, payload text NOT NULL,
  bytes int NOT NULL CHECK(bytes BETWEEN 1 AND 2097152), CHECK(octet_length(payload)=bytes)
);
CREATE TABLE business.tool_calls (
  turn_id text NOT NULL REFERENCES business.turns(id), call_id text NOT NULL, action_hash text NOT NULL,
  status text NOT NULL CHECK(status IN ('pending','completed')), result text,
  PRIMARY KEY(turn_id,call_id)
);
CREATE TABLE business.reports (
  id text PRIMARY KEY, turn_id text NOT NULL REFERENCES business.turns(id), call_id text NOT NULL,
  title text NOT NULL, UNIQUE(turn_id,call_id)
);

ALTER TABLE business.attempts ADD COLUMN started_at timestamptz NOT NULL DEFAULT clock_timestamp();
-- Legacy conversations are readable; new Turns receive explicit policy at admission.
INSERT INTO business.grants(conversation_id,scopes) SELECT id,'[]'::jsonb FROM business.conversations ON CONFLICT DO NOTHING;
