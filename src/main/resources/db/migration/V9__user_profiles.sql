ALTER TABLE memory.owners
  ADD COLUMN profile_revision bigint NOT NULL DEFAULT 0 CHECK(profile_revision >= 0);
ALTER TABLE memory.sources
  ADD COLUMN profile_revision bigint NOT NULL DEFAULT 0 CHECK(profile_revision >= 0);

-- A single authoritative document. Turns bind its revision without copying its body.
CREATE TABLE memory.user_profiles (
  tenant text NOT NULL,
  subject text NOT NULL,
  epoch bigint NOT NULL CHECK(epoch >= 1),
  revision bigint NOT NULL CHECK(revision >= 1),
  markdown text NOT NULL CHECK(octet_length(markdown) <= 8192),
  updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY(tenant,subject),
  FOREIGN KEY(tenant,subject) REFERENCES memory.owners(tenant,subject) ON DELETE CASCADE
);

-- Before profiles existed, memory-disabled Turns had no source row. Preserve their execution
-- and artifact privacy bindings only where the current database proves no forget has occurred.
-- This migration does not replace independent forget-ledger replay when restoring an old backup.
INSERT INTO memory.owners(tenant,subject)
SELECT DISTINCT tenant,subject FROM business.turns
ON CONFLICT(tenant,subject) DO NOTHING;

INSERT INTO memory.sources(id,tenant,subject,epoch,content,status,profile_revision)
SELECT t.id,t.tenant,t.subject,1,NULL,'profile_only',0
FROM business.turns t
JOIN memory.owners o ON o.tenant=t.tenant AND o.subject=t.subject
WHERE o.epoch=1 AND o.profile_revision=0 AND NOT o.purge_pending
ON CONFLICT(id) DO NOTHING;
