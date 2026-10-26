-- Local ASK/clarification leaves reuse root interactions and effect receipts, never a child queue.
ALTER TABLE business.child_invocations DROP CONSTRAINT child_invocations_leaf_profile_check;
ALTER TABLE business.child_invocations ADD CONSTRAINT child_invocations_leaf_profile_check
  CHECK (leaf_profile IN ('legacy','read_only','controlled'));
ALTER TABLE business.child_invocations DROP CONSTRAINT child_invocations_target_binding_check;
ALTER TABLE business.child_invocations ADD CONSTRAINT child_invocations_target_binding_check CHECK (
  (target='market_research' AND leaf_profile='legacy' AND target_agent_id IS NULL
    AND target_release_hash IS NULL AND scopes='["market:read"]'::jsonb)
  OR (target='private_agent' AND target_agent_id IS NOT NULL AND target_release_hash IS NOT NULL
    AND ((leaf_profile='legacy' AND ((scopes='[]'::jsonb AND tool_calls=0)
      OR (scopes='["market:read"]'::jsonb AND tool_calls BETWEEN 0 AND 1)))
    OR (leaf_profile IN ('read_only','controlled') AND jsonb_typeof(scopes)='array' AND tool_calls BETWEEN 0 AND 10))));
CREATE INDEX child_interaction_bridge_lookup ON business.capability_steps(turn_id,step)
  WHERE namespace LIKE 'child:%' AND step LIKE 'interaction:%';

CREATE OR REPLACE FUNCTION agent_runtime.child_invocation_bindings()
RETURNS TABLE (
  tenant text,
  subject text,
  root_turn_id text,
  call_id text,
  target text,
  target_agent_id text,
  target_release_hash text,
  binding_valid boolean
)
LANGUAGE sql STABLE PARALLEL SAFE SECURITY INVOKER
AS $$
  SELECT t.tenant, t.subject, i.root_turn_id, i.call_id, i.target,
    i.target_agent_id, i.target_release_hash,
    CASE
      WHEN i.target='market_research' THEN t.id IS NOT NULL
        AND i.target_agent_id IS NULL AND i.target_release_hash IS NULL
        AND i.scopes='["market:read"]'::jsonb
      WHEN i.target='private_agent' THEN COALESCE(
        t.id IS NOT NULL AND length(btrim(i.target_agent_id))>0
        AND length(btrim(i.target_release_hash))>0
        AND a.id IS NOT NULL
        AND m.manifest_format='agent-v3' AND m.manifest->>'format'='agent-release/3'
        AND m.manifest->>'agentId'=i.target_agent_id
        AND m.manifest->>'tenant'=a.tenant AND m.manifest->>'subject'=a.subject
        AND (
          -- agent-leaf/1 原有空工具身份不变，不能因新档位放宽而借到行情权限。
          (i.leaf_profile='legacy' AND i.scopes='[]'::jsonb AND i.tool_calls=0
            AND m.manifest#>'{plan,requestedCapabilities}'='[]'::jsonb
            AND m.manifest#>'{plan,capabilities}'='[]'::jsonb
            AND m.manifest#>'{plan,definition,scopes}'='[]'::jsonb)
          OR
          -- 发布 resolver 按能力 ID 排序：snapshot 在前，隐含 reader 在后。
          -- reader 自身 scopes=[]，不能错误要求目标额外拥有 artifact:read。
          -- 新委派契约只执行 snapshot；reader 是既有发布闭包，不能被子运行时装配。
          -- 数组长度与每项权限/依赖都精确匹配，额外工具、重复项和权限扩张均不算合法绑定。
          (i.leaf_profile='legacy' AND i.scopes='["market:read"]'::jsonb AND i.tool_calls BETWEEN 0 AND 1
            AND m.manifest#>'{plan,requestedCapabilities}'='["function:market_snapshot"]'::jsonb
            AND m.manifest#>'{plan,definition,scopes}'='["market:read"]'::jsonb
            AND jsonb_typeof(m.manifest#>'{plan,definition,maxToolCalls}')='number'
            AND m.manifest#>>'{plan,definition,maxToolCalls}' ~ '^[1-9][0-9]*$'
            AND CASE WHEN jsonb_typeof(m.manifest#>'{plan,capabilities}')='array'
              THEN jsonb_array_length(m.manifest#>'{plan,capabilities}')=2 ELSE false END
            AND m.manifest#>>'{plan,capabilities,0,id}'='function:market_snapshot'
            AND m.manifest#>>'{plan,capabilities,0,kind}'='function'
            AND m.manifest#>>'{plan,capabilities,0,name}'='market_snapshot'
            AND m.manifest#>'{plan,capabilities,0,scopes}'='["market:read"]'::jsonb
            AND m.manifest#>'{plan,capabilities,0,dependencies}'='["function:read_artifact_range"]'::jsonb
            AND m.manifest#>>'{plan,capabilities,1,id}'='function:read_artifact_range'
            AND m.manifest#>>'{plan,capabilities,1,kind}'='function'
            AND m.manifest#>>'{plan,capabilities,1,name}'='read_artifact_range'
            AND m.manifest#>'{plan,capabilities,1,scopes}'='[]'::jsonb
            AND m.manifest#>'{plan,capabilities,1,dependencies}'='[]'::jsonb)
          OR (i.leaf_profile IN ('read_only','controlled') AND i.tool_calls BETWEEN 0 AND 10
            AND i.scopes=m.manifest#>'{plan,definition,scopes}'
            AND jsonb_typeof(i.scopes)='array'
            AND jsonb_typeof(m.manifest#>'{plan,requestedCapabilities}')='array'
            AND jsonb_typeof(m.manifest#>'{plan,capabilities}')='array'
            AND m.manifest#>'{plan,capabilities}' <> '[]'::jsonb
            AND NOT EXISTS (SELECT 1 FROM jsonb_array_elements(CASE
                WHEN jsonb_typeof(m.manifest#>'{plan,capabilities}')='array'
                THEN m.manifest#>'{plan,capabilities}' ELSE '[]'::jsonb END) c
              WHERE NOT (c->>'kind' IN ('skill','mcp') OR (c->>'kind'='function'
                AND (c->>'name' IN ('market_snapshot','search_catalog','read_artifact','read_artifact_range')
                  OR (i.leaf_profile='controlled' AND c->>'name' IN ('request_city','create_report')))))))
        ), false)
      ELSE false
    END
  FROM business.child_invocations i
  LEFT JOIN business.turns t ON t.id=i.root_turn_id
  LEFT JOIN control_plane.agents a ON a.id=i.target_agent_id
  LEFT JOIN control_plane.release_manifests m ON m.release_hash=i.target_release_hash
$$;
