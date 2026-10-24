-- Run with psql -X -qAt -v ON_ERROR_STOP=1, against a quiesced database.
-- This is an administrative, all-owner observation. Store the output privately.
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL statement_timeout = '30s';
-- 执行副本只汇总类别和归属，不导出 SDK、工具或步骤正文；同一只读快照中只计算一次。
WITH runtime_residuals AS MATERIALIZED (
  SELECT * FROM agent_runtime.unsafe_execution_data()
), runtime_counts AS (
  -- 每个 owner 的四类计数只聚合一次，避免逐个用户反复扫描同一份残留集合。
  SELECT tenant, subject,
    count(*) FILTER (WHERE kind='sdk_states') AS unsafe_sdk_states,
    count(*) FILTER (WHERE kind='capability_steps') AS unsafe_capability_steps,
    count(*) FILTER (WHERE kind='child_invocations') AS unsafe_child_invocations,
    count(*) FILTER (WHERE kind='tool_artifacts') AS unsafe_tool_artifacts
  FROM runtime_residuals GROUP BY tenant,subject
), owners AS (
  SELECT o.tenant, o.subject, o.epoch, o.purge_pending,
    COALESCE((SELECT jsonb_agg(jsonb_build_object(
      'id', a.id, 'epoch', a.epoch, 'action', a.action,
      'at', to_char(a.created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')) ORDER BY a.id)
      FROM memory.audit a WHERE a.tenant=o.tenant AND a.subject=o.subject
      AND a.action IN ('forget','purge_complete')), '[]'::jsonb) AS events,
    (SELECT count(*) FROM memory.user_profiles p WHERE p.tenant=o.tenant AND p.subject=o.subject
      AND (p.epoch<>o.epoch OR p.epoch<1 OR p.revision<1 OR p.revision<>o.profile_revision))
      + CASE WHEN o.profile_revision<0 OR (o.profile_revision>0 AND NOT EXISTS
        (SELECT 1 FROM memory.user_profiles p WHERE p.tenant=o.tenant AND p.subject=o.subject
          AND p.epoch=o.epoch AND p.revision=o.profile_revision)) THEN 1 ELSE 0 END AS unsafe_profiles,
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
    (SELECT count(*) FROM memory.turn_snapshots p LEFT JOIN memory.sources s ON s.id=p.turn_id
      WHERE p.tenant=o.tenant AND p.subject=o.subject
      AND (p.epoch<>o.epoch OR s.id IS NULL OR s.tenant<>p.tenant OR s.subject<>p.subject
        OR s.epoch<>p.epoch OR s.profile_revision<>p.profile_revision OR s.status='forgotten')) AS unsafe_snapshots,
    (SELECT count(*) FROM agent_runtime.work_contexts w LEFT JOIN memory.sources s ON s.id=w.turn_id
      WHERE w.tenant=o.tenant AND w.subject=o.subject
      AND (w.epoch<>o.epoch OR s.id IS NULL OR s.tenant<>w.tenant OR s.subject<>w.subject
        OR s.epoch<>w.epoch OR s.status='forgotten')) AS unsafe_work_contexts,
    (SELECT count(*) FROM memory.jobs j WHERE j.tenant=o.tenant AND j.subject=o.subject
      AND (j.epoch>o.epoch OR j.epoch<1
        OR (j.epoch<o.epoch AND (j.status NOT IN ('forgotten','obsolete')
          OR j.lease_token IS NOT NULL OR j.lease_until IS NOT NULL)))) AS unsafe_jobs,
    COALESCE(r.unsafe_sdk_states,0) AS unsafe_sdk_states,
    COALESCE(r.unsafe_capability_steps,0) AS unsafe_capability_steps,
    COALESCE(r.unsafe_child_invocations,0) AS unsafe_child_invocations,
    COALESCE(r.unsafe_tool_artifacts,0) AS unsafe_tool_artifacts,
    (SELECT count(*) FROM memory.audit a WHERE a.tenant=o.tenant AND a.subject=o.subject
      AND a.action IN ('forget','purge_complete') AND a.target<>'all') AS invalid_audit_targets
  FROM memory.owners o LEFT JOIN runtime_counts r USING(tenant,subject)
), referenced_owners AS (
  SELECT tenant,subject FROM memory.sources UNION SELECT tenant,subject FROM memory.facts
  UNION SELECT tenant,subject FROM memory.projections UNION SELECT tenant,subject FROM memory.jobs
  UNION SELECT tenant,subject FROM memory.audit UNION SELECT tenant,subject FROM memory.operations
  UNION SELECT tenant,subject FROM memory.user_profiles
  UNION SELECT tenant,subject FROM memory.turn_snapshots
  UNION SELECT tenant,subject FROM agent_runtime.work_contexts
  UNION SELECT tenant,subject FROM runtime_residuals WHERE tenant IS NOT NULL AND subject IS NOT NULL
)
SELECT jsonb_build_object(
  'schema_version', 3, 'scope', 'all_memory_owners',
  'consistency', 'repeatable_read_read_only',
  'captured_at', to_char(transaction_timestamp() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
  'orphan_owners', (SELECT count(*) FROM referenced_owners r WHERE NOT EXISTS
    (SELECT 1 FROM memory.owners o WHERE o.tenant=r.tenant AND o.subject=r.subject)),
  'orphan_runtime_rows', (SELECT count(*) FROM runtime_residuals r WHERE NOT EXISTS
    (SELECT 1 FROM memory.owners o WHERE o.tenant=r.tenant AND o.subject=r.subject)),
  'owners', COALESCE((SELECT jsonb_agg(to_jsonb(o) ORDER BY tenant,subject) FROM owners o), '[]'::jsonb)
);
COMMIT;
