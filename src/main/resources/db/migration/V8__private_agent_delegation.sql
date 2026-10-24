-- 深度为一的私有 Agent 同步委派复用已有子调用身份、父预算和执行隐私边界。
-- 不增加调用表、子队列或租约。目标 Agent 与发布摘要只在首次受理时冻结，重放不读取当前部署。
ALTER TABLE business.child_invocations
  ADD COLUMN target_agent_id text REFERENCES control_plane.agents(id),
  ADD COLUMN target_release_hash text REFERENCES control_plane.release_manifests(release_hash);

ALTER TABLE business.child_invocations
  DROP CONSTRAINT child_invocations_target_check,
  DROP CONSTRAINT child_invocations_scopes_check;

-- 原有研究调用继续使用原权限、原工具上限及原 SDK scope，不补造目标发布。
-- 私有叶子只允许显式消息与模型调用：无工具权限、工具计数必须为零；四次模型上限沿用原约束。
ALTER TABLE business.child_invocations
  ADD CONSTRAINT child_invocations_target_binding_check CHECK (
    (target='market_research' AND target_agent_id IS NULL AND target_release_hash IS NULL
      AND scopes='["market:read"]'::jsonb)
    OR (target='private_agent' AND target_agent_id IS NOT NULL AND target_release_hash IS NOT NULL
      AND scopes='[]'::jsonb AND tool_calls=0));

COMMENT ON COLUMN business.child_invocations.target_agent_id IS
  '私有叶子 Agent 的冻结身份；固定市场研究保持 NULL';
COMMENT ON COLUMN business.child_invocations.target_release_hash IS
  '私有叶子首次受理的冻结发布；不能随当前部署指针变化';

-- private 调用身份不能通过原地 UPDATE 改绑；模型/工具计数和完成状态仍可正常 CAS 更新。
-- 只保护涉及 private 的绑定，保持已有固定研究调用的损坏检测/回归入口，不改变其历史行为。
CREATE FUNCTION business.guard_private_child_identity()
RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER
AS $$
BEGIN
  IF (OLD.target='private_agent' OR NEW.target='private_agent') AND (
    NEW.root_turn_id IS DISTINCT FROM OLD.root_turn_id
    OR NEW.call_id IS DISTINCT FROM OLD.call_id
    OR NEW.target IS DISTINCT FROM OLD.target
    OR NEW.input_hash IS DISTINCT FROM OLD.input_hash
    OR NEW.scopes IS DISTINCT FROM OLD.scopes
    OR NEW.target_agent_id IS DISTINCT FROM OLD.target_agent_id
    OR NEW.target_release_hash IS DISTINCT FROM OLD.target_release_hash) THEN
    RAISE EXCEPTION 'private_child_identity_immutable' USING ERRCODE='23514';
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER private_child_identity_immutable BEFORE UPDATE ON business.child_invocations
  FOR EACH ROW EXECUTE FUNCTION business.guard_private_child_identity();

-- 集合映射只读取已保存的目标和不可变发布元数据，不读取 current deployment。
-- helper 可由 planner 内联并下推 root owner 条件；不逐行调用带查询的标量校验函数。
-- SQL 检查仅供恢复/残留识别：清单摘要、运行时兼容及父计划允许哪些目标仍由 Java 执行边界核验。
-- 即使 private 目标无效，也保留原始目标字段，以便按原 scope 定位和擦除 SDK 正文。
CREATE FUNCTION agent_runtime.child_invocation_bindings()
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
        AND length(btrim(i.target_release_hash))>0 AND i.scopes='[]'::jsonb AND i.tool_calls=0
        AND a.id IS NOT NULL AND a.tenant=t.tenant AND a.subject=t.subject
        AND m.manifest_format='agent-v3' AND m.manifest->>'format'='agent-release/3'
        AND m.manifest->>'agentId'=i.target_agent_id
        AND m.manifest->>'tenant'=t.tenant AND m.manifest->>'subject'=t.subject
        AND m.manifest#>'{plan,requestedCapabilities}'='[]'::jsonb
        AND m.manifest#>'{plan,capabilities}'='[]'::jsonb
        AND m.manifest#>'{plan,definition,scopes}'='[]'::jsonb, false)
      ELSE false
    END
  FROM business.child_invocations i
  LEFT JOIN business.turns t ON t.id=i.root_turn_id
  LEFT JOIN control_plane.agents a ON a.id=i.target_agent_id
  LEFT JOIN control_plane.release_manifests m ON m.release_hash=i.target_release_hash
$$;

COMMENT ON FUNCTION agent_runtime.child_invocation_bindings() IS
  '集合核对研究/同 owner 空能力私有叶子绑定；仅使用冻结目标与清单，不读取当前部署';

-- 保持 V7 返回契约，原 erase/unsafe 调用方无需增加 SDK 归属字段或第二套隐私版本。
-- 根与固定研究的哈希算法逐字保留；私有 scope 使用冻结 target Agent/release，不能误用根发布。
CREATE OR REPLACE FUNCTION agent_runtime.execution_scopes()
RETURNS TABLE (
  tenant text,
  subject text,
  root_turn_id text,
  epoch bigint,
  source_status text,
  namespace text,
  user_id text,
  session_id text,
  invocation_bound boolean
)
LANGUAGE sql STABLE PARALLEL SAFE SECURITY INVOKER
AS $$
  WITH attempts AS NOT MATERIALIZED (
    SELECT t.tenant, t.subject, t.id AS root_turn_id, t.agent_id, t.release_hash,
      s.epoch, s.status AS source_status,
      agent_runtime.scope_hash(ARRAY[t.conversation_id, t.id, c.id, a.id]) AS conversation_scope
    FROM business.turns t
    JOIN business.commands c ON c.turn_id=t.id
    JOIN business.attempts a ON a.command_id=c.id
    LEFT JOIN memory.sources s ON s.id=t.id AND s.tenant=t.tenant AND s.subject=t.subject
  ), roots AS NOT MATERIALIZED (
    SELECT a.tenant, a.subject, a.root_turn_id, a.release_hash, a.epoch, a.source_status,
      agent_runtime.scope_hash(ARRAY[
        'financeclaw-state-v1', a.tenant, a.subject, a.agent_id, a.release_hash]) AS namespace,
      agent_runtime.scope_hash(ARRAY[a.tenant, a.subject]) AS user_id,
      agent_runtime.scope_hash(ARRAY[a.conversation_scope, a.agent_id, a.release_hash]) AS session_id
    FROM attempts a
  ), child_calls AS NOT MATERIALIZED (
    SELECT r.tenant, r.subject, r.root_turn_id, r.release_hash, r.epoch, r.source_status,
      r.namespace, r.user_id, r.session_id, b.call_id, b.target,
      b.target_agent_id, b.target_release_hash, b.binding_valid AS invocation_bound
    FROM roots r
    JOIN agent_runtime.child_invocation_bindings() b ON b.root_turn_id=r.root_turn_id
      AND b.tenant=r.tenant AND b.subject=r.subject
    UNION ALL
    -- 无绑定的旧 child:* 步骤仅保留研究 scope 清理线索；不猜测新的私有目标或恢复预算。
    -- 私有绑定和步骤都丢失时，未知 live state 继续由 owner/global 残留门禁阻断。
    SELECT DISTINCT r.tenant, r.subject, r.root_turn_id, r.release_hash, r.epoch, r.source_status,
      r.namespace, r.user_id, r.session_id, substring(s.namespace FROM 7), 'market_research',
      NULL::text, NULL::text, false
    FROM roots r
    JOIN business.capability_steps s ON s.turn_id=r.root_turn_id AND s.namespace LIKE 'child:%'
    WHERE NOT EXISTS (
      SELECT 1 FROM business.child_invocations i
      WHERE i.root_turn_id=r.root_turn_id AND i.call_id=substring(s.namespace FROM 7))
  )
  SELECT r.tenant, r.subject, r.root_turn_id, r.epoch, r.source_status,
    r.namespace, r.user_id, r.session_id, true
  FROM roots r
  UNION ALL
  SELECT c.tenant, c.subject, c.root_turn_id, c.epoch, c.source_status,
    agent_runtime.scope_hash(ARRAY[
      'financeclaw-state-v1', c.tenant, c.subject, 'market_research', c.release_hash]),
    c.user_id,
    agent_runtime.scope_hash(ARRAY[
      agent_runtime.scope_hash(ARRAY[
        agent_runtime.scope_hash(ARRAY[c.namespace, c.user_id, c.session_id]),
        'market_research', c.call_id]),
      'market_research', c.release_hash]),
    c.invocation_bound
  FROM child_calls c WHERE c.target='market_research'
  UNION ALL
  SELECT c.tenant, c.subject, c.root_turn_id, c.epoch, c.source_status,
    agent_runtime.scope_hash(ARRAY[
      'financeclaw-state-v1', c.tenant, c.subject, c.target_agent_id, c.target_release_hash]),
    c.user_id,
    agent_runtime.scope_hash(ARRAY[
      agent_runtime.scope_hash(ARRAY[
        agent_runtime.scope_hash(ARRAY[c.namespace, c.user_id, c.session_id]),
        'private_agent', c.call_id, c.target_agent_id, c.target_release_hash]),
      c.target_agent_id, c.target_release_hash]),
    c.invocation_bound
  FROM child_calls c WHERE c.target='private_agent'
$$;

COMMENT ON FUNCTION agent_runtime.execution_scopes() IS
  '按全部根 Attempt 及冻结研究/私有叶子绑定派生 scope；失效绑定保留定位但不授执行权';

-- 私有调用无 SDK 状态时，其调用行和结果步骤仍必须接受目标绑定有效性检查。
-- 不能只验证同 root/call 存在任意记录，否则 foreign owner 或非空能力清单可漏过恢复核验。
-- 这仍是离线/行政集合核验，正文不出库；普通 owner 擦除沿用 V7 的索引及集合候选路径。
CREATE OR REPLACE FUNCTION agent_runtime.unsafe_execution_data()
RETURNS TABLE (tenant text, subject text, kind text)
LANGUAGE sql STABLE PARALLEL SAFE SECURITY INVOKER
AS $$
  WITH scopes AS MATERIALIZED (
    SELECT * FROM agent_runtime.execution_scopes()
  ), bindings AS MATERIALIZED (
    SELECT * FROM agent_runtime.child_invocation_bindings()
  )
  SELECT COALESCE(x.tenant, h.tenant), COALESCE(x.subject, h.subject), 'sdk_states'::text
  FROM agent_runtime.states s
  LEFT JOIN scopes x ON x.namespace=s.namespace AND x.user_id=s.user_id AND x.session_id=s.session_id
  LEFT JOIN memory.owners o ON o.tenant=x.tenant AND o.subject=x.subject
  LEFT JOIN memory.owners h ON x.root_turn_id IS NULL
    AND agent_runtime.scope_hash(ARRAY[h.tenant, h.subject])=s.user_id
  WHERE NOT s.deleted AND (x.root_turn_id IS NULL OR NOT x.invocation_bound
    OR x.epoch IS NULL OR x.epoch<1 OR x.source_status IS NULL OR x.source_status='forgotten'
    OR o.tenant IS NULL OR x.epoch<>o.epoch)
  UNION ALL
  SELECT t.tenant, t.subject, 'capability_steps'::text
  FROM business.capability_steps c
  LEFT JOIN business.turns t ON t.id=c.turn_id
  LEFT JOIN memory.sources s ON s.id=t.id AND s.tenant=t.tenant AND s.subject=t.subject
  LEFT JOIN memory.owners o ON o.tenant=t.tenant AND o.subject=t.subject
  LEFT JOIN bindings b ON c.namespace LIKE 'child:%' AND b.root_turn_id=c.turn_id
    AND b.call_id=substring(c.namespace FROM 7)
  WHERE t.id IS NULL OR s.id IS NULL OR s.epoch<1 OR o.tenant IS NULL OR s.epoch<>o.epoch
    OR s.status='forgotten'
    OR (c.namespace LIKE 'child:%' AND (b.root_turn_id IS NULL OR NOT b.binding_valid))
  UNION ALL
  SELECT b.tenant, b.subject, 'child_invocations'::text
  FROM bindings b
  LEFT JOIN memory.sources s ON s.id=b.root_turn_id AND s.tenant=b.tenant AND s.subject=b.subject
  LEFT JOIN memory.owners o ON o.tenant=b.tenant AND o.subject=b.subject
  WHERE NOT b.binding_valid OR s.id IS NULL OR s.epoch<1 OR o.tenant IS NULL
    OR s.epoch<>o.epoch OR s.status='forgotten'
  UNION ALL
  SELECT COALESCE(t.tenant, a.tenant), COALESCE(t.subject, a.subject), 'tool_artifacts'::text
  FROM business.artifacts a
  LEFT JOIN business.turns t ON t.id=a.turn_id
  LEFT JOIN memory.sources s ON s.id=t.id AND s.tenant=t.tenant AND s.subject=t.subject
  LEFT JOIN memory.owners o ON o.tenant=t.tenant AND o.subject=t.subject
  WHERE a.privacy_bound AND (t.id IS NULL OR s.id IS NULL OR s.epoch<1
    OR o.tenant IS NULL OR s.epoch<>o.epoch OR s.status='forgotten'
    OR a.tenant<>t.tenant OR a.subject<>t.subject
    OR a.conversation_id<>t.conversation_id OR a.release_hash<>t.release_hash)
  UNION ALL
  -- 冲突 Artifact 的声明 owner 与根 owner 两侧都要有残留记录，延续 V7 的恢复契约。
  SELECT a.tenant, a.subject, 'tool_artifacts'::text
  FROM business.artifacts a
  JOIN business.turns t ON t.id=a.turn_id
  WHERE a.privacy_bound AND (a.tenant<>t.tenant OR a.subject<>t.subject)
$$;

COMMENT ON FUNCTION agent_runtime.unsafe_execution_data() IS
  '集合核验执行正文及研究/空能力私有叶子绑定；无 SDK 的失效调用与步骤同样计残留';
