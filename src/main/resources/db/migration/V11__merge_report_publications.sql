-- The local report is the governed effect. Keep the entity and its exact replay receipt together.
-- Receipt-only and report-only historical records are preserved without inventing missing data.
ALTER TABLE business.reports DROP CONSTRAINT reports_pkey;
ALTER TABLE business.reports DROP CONSTRAINT reports_turn_id_call_id_key;
ALTER TABLE business.reports ALTER COLUMN id DROP NOT NULL;
ALTER TABLE business.reports ALTER COLUMN title DROP NOT NULL;
ALTER TABLE business.reports ADD CONSTRAINT reports_pkey PRIMARY KEY(turn_id,call_id);
ALTER TABLE business.reports ADD CONSTRAINT reports_id_key UNIQUE(id);
ALTER TABLE business.reports ADD COLUMN action_hash text;
ALTER TABLE business.reports ADD COLUMN status text NOT NULL DEFAULT 'legacy_untracked';
ALTER TABLE business.reports ADD COLUMN result text;

INSERT INTO business.reports(turn_id,call_id,action_hash,status,result)
SELECT turn_id,call_id,action_hash,status,result FROM business.tool_calls
ON CONFLICT(turn_id,call_id) DO UPDATE SET
  action_hash=excluded.action_hash,status=excluded.status,result=excluded.result;

ALTER TABLE business.reports ALTER COLUMN status DROP DEFAULT;
ALTER TABLE business.reports ADD CONSTRAINT reports_status_check
  CHECK(status IN ('pending','completed','legacy_untracked'));
ALTER TABLE business.reports ADD CONSTRAINT reports_entity_check
  CHECK((id IS NULL)=(title IS NULL));
ALTER TABLE business.reports ADD CONSTRAINT reports_receipt_identity_check
  CHECK((status='legacy_untracked' AND action_hash IS NULL AND result IS NULL)
    OR (status IN ('pending','completed') AND action_hash IS NOT NULL));
CREATE INDEX reports_uncertain_turn ON business.reports(turn_id)
  WHERE status<>'completed' OR result IS NULL;
COMMENT ON COLUMN business.reports.id IS
  'Original report ID; NULL for a historical effect receipt with no report entity.';
COMMENT ON COLUMN business.reports.status IS
  'legacy_untracked preserves a report without a historical receipt; it requires reconciliation.';
COMMENT ON COLUMN business.reports.result IS
  'Exact historical replay bytes; never reconstructed from the entity ID or title.';
DROP TABLE business.tool_calls;
