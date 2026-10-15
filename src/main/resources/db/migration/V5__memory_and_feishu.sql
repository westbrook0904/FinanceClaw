CREATE SCHEMA memory;
CREATE TABLE memory.owners (
  tenant text NOT NULL, subject text NOT NULL, epoch bigint NOT NULL DEFAULT 1,
  purge_pending boolean NOT NULL DEFAULT false, PRIMARY KEY(tenant,subject)
);
CREATE TABLE memory.sources (
  id text PRIMARY KEY REFERENCES business.turns(id) ON DELETE CASCADE,
  tenant text NOT NULL, subject text NOT NULL, epoch bigint NOT NULL,
  content text, status text NOT NULL DEFAULT 'admitted',
  created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE memory.facts (
  id text PRIMARY KEY, source_id text NOT NULL REFERENCES memory.sources(id) ON DELETE CASCADE,
  tenant text NOT NULL, subject text NOT NULL, epoch bigint NOT NULL,
  key text NOT NULL, content text, evidence text, revision bigint NOT NULL DEFAULT 1,
  status text NOT NULL CHECK(status IN ('candidate','active','rejected','superseded','forgotten')),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(), UNIQUE(source_id,key)
);
CREATE UNIQUE INDEX memory_active_key ON memory.facts(tenant,subject,key) WHERE status='active';
CREATE TABLE memory.recalls (
  turn_id text NOT NULL REFERENCES business.turns(id) ON DELETE CASCADE,
  fact_id text NOT NULL REFERENCES memory.facts(id) ON DELETE CASCADE,
  revision bigint NOT NULL, epoch bigint NOT NULL, PRIMARY KEY(turn_id,fact_id)
);
CREATE TABLE memory.jobs (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, kind text NOT NULL CHECK(kind IN ('extract','index')),
  key_id text NOT NULL, tenant text NOT NULL, subject text NOT NULL, epoch bigint NOT NULL,
  revision bigint NOT NULL DEFAULT 1, model_revision text NOT NULL DEFAULT '',
  status text NOT NULL DEFAULT 'pending', attempts int NOT NULL DEFAULT 0,
  lease_token text, lease_until timestamptz, next_attempt_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  error_code text, UNIQUE(kind,key_id,revision,model_revision)
);
CREATE INDEX memory_job_queue ON memory.jobs(next_attempt_at,id) WHERE status IN ('pending','running');
-- Portable canonical vector storage. The optional pgvector migration adds the search index.
CREATE TABLE memory.embedding_profiles (
  revision text PRIMARY KEY, fingerprint text NOT NULL, model text NOT NULL, dimensions int NOT NULL CHECK(dimensions=1024)
);
CREATE TABLE memory.projections (
  fact_id text NOT NULL REFERENCES memory.facts(id) ON DELETE CASCADE,
  tenant text NOT NULL, subject text NOT NULL, epoch bigint NOT NULL, revision bigint NOT NULL,
  model_revision text NOT NULL, dimensions int NOT NULL CHECK(dimensions=1024),
  embedding real[] NOT NULL CHECK(array_length(embedding,1)=1024), PRIMARY KEY(fact_id,model_revision)
);
CREATE TABLE memory.operations (
  tenant text NOT NULL, subject text NOT NULL, key text NOT NULL, request_hash text NOT NULL,
  result text NOT NULL, PRIMARY KEY(tenant,subject,key)
);
CREATE TABLE memory.audit (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, tenant text NOT NULL, subject text NOT NULL,
  epoch bigint NOT NULL, action text NOT NULL, target text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE SCHEMA channel;
CREATE TABLE channel.link_codes (
  code_hash text PRIMARY KEY, tenant text NOT NULL, subject text NOT NULL,
  conversation_id text NOT NULL REFERENCES business.conversations(id),
  expires_at timestamptz NOT NULL DEFAULT clock_timestamp()+interval '5 minutes', used boolean NOT NULL DEFAULT false
);
CREATE TABLE channel.bindings (
  id text PRIMARY KEY, tenant_key text NOT NULL, open_id text NOT NULL, chat_id text NOT NULL,
  tenant text NOT NULL, subject text NOT NULL, conversation_id text NOT NULL REFERENCES business.conversations(id),
  revision bigint NOT NULL DEFAULT 1, muted boolean NOT NULL DEFAULT false, active_turn text,
  UNIQUE(tenant_key,open_id,chat_id)
);
CREATE TABLE channel.inbox (
  event_id text PRIMARY KEY, binding_id text NOT NULL REFERENCES channel.bindings(id),
  request_hash text NOT NULL, receipt text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE channel.notices (
  id text PRIMARY KEY, turn_id text NOT NULL REFERENCES business.turns(id),
  binding_id text NOT NULL REFERENCES channel.bindings(id), binding_revision bigint NOT NULL,
  revision bigint NOT NULL, payload text NOT NULL, status text NOT NULL DEFAULT 'pending'
    CHECK(status IN ('pending','sending','submitted_unverified','unknown','suppressed')),
  reason text, started_at timestamptz, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE(turn_id,revision)
);
CREATE INDEX channel_pending_notice ON channel.notices(created_at) WHERE status='pending';
