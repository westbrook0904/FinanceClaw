-- Run with psql -X -qAt -v ON_ERROR_STOP=1, against a quiesced database.
-- This is an administrative, all-owner observation. Store the output privately.
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL statement_timeout = '30s';
WITH owners AS (
  SELECT o.tenant, o.subject, o.epoch, o.purge_pending,
    COALESCE((SELECT jsonb_agg(jsonb_build_object(
      'id', a.id, 'epoch', a.epoch, 'action', a.action,
      'at', to_char(a.created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')) ORDER BY a.id)
      FROM memory.audit a WHERE a.tenant=o.tenant AND a.subject=o.subject
      AND a.action IN ('forget','purge_complete')), '[]'::jsonb) AS events,
    (SELECT count(*) FROM memory.sources s WHERE s.tenant=o.tenant AND s.subject=o.subject
      AND (s.epoch>o.epoch OR s.epoch<1
        OR ((s.epoch<o.epoch OR s.status='forgotten')
          AND (s.content IS NOT NULL OR s.status<>'forgotten')))) AS unsafe_sources,
    (SELECT count(*) FROM memory.facts f WHERE f.tenant=o.tenant AND f.subject=o.subject
      AND (f.epoch>o.epoch OR f.epoch<1
        OR ((f.epoch<o.epoch OR f.status='forgotten')
          AND (f.content IS NOT NULL OR f.evidence IS NOT NULL OR f.status<>'forgotten')))) AS unsafe_facts,
    (SELECT count(*) FROM memory.projections p LEFT JOIN memory.facts f ON f.id=p.fact_id
      WHERE p.tenant=o.tenant AND p.subject=o.subject
      AND (p.epoch<>o.epoch OR f.id IS NULL OR f.tenant<>p.tenant OR f.subject<>p.subject
        OR f.epoch<>p.epoch OR f.status='forgotten')) AS unsafe_projections,
    (SELECT count(*) FROM memory.recalls r JOIN memory.facts f ON f.id=r.fact_id
      WHERE f.tenant=o.tenant AND f.subject=o.subject
      AND (r.epoch<>o.epoch OR f.epoch<>o.epoch OR f.status='forgotten')) AS unsafe_recalls,
    (SELECT count(*) FROM memory.jobs j WHERE j.tenant=o.tenant AND j.subject=o.subject
      AND (j.epoch>o.epoch OR j.epoch<1
        OR (j.epoch<o.epoch AND (j.status NOT IN ('forgotten','obsolete')
          OR j.lease_token IS NOT NULL OR j.lease_until IS NOT NULL)))) AS unsafe_jobs,
    (SELECT count(*) FROM memory.audit a WHERE a.tenant=o.tenant AND a.subject=o.subject
      AND a.action IN ('forget','purge_complete') AND a.target<>'all') AS invalid_audit_targets
  FROM memory.owners o
), referenced_owners AS (
  SELECT tenant,subject FROM memory.sources UNION SELECT tenant,subject FROM memory.facts
  UNION SELECT tenant,subject FROM memory.projections UNION SELECT tenant,subject FROM memory.jobs
  UNION SELECT tenant,subject FROM memory.audit UNION SELECT tenant,subject FROM memory.operations
)
SELECT jsonb_build_object(
  'schema_version', 1, 'scope', 'all_memory_owners',
  'consistency', 'repeatable_read_read_only',
  'captured_at', to_char(transaction_timestamp() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
  'orphan_owners', (SELECT count(*) FROM referenced_owners r WHERE NOT EXISTS
    (SELECT 1 FROM memory.owners o WHERE o.tenant=r.tenant AND o.subject=r.subject)),
  'owners', COALESCE((SELECT jsonb_agg(to_jsonb(o) ORDER BY tenant,subject) FROM owners o), '[]'::jsonb)
);
COMMIT;
