-- 执行过程也属于隐私资料：SDK 上下文、确定性步骤及工具文件不能只靠读取门禁隐藏。
-- 本文件只增加集合函数，不增加表、调度器或独立隐私版本。身份仍由根 Turn、历史 Attempt
-- 和同 owner 的 memory.sources 派生；已遗忘的 SDK 状态保留无正文墓碑，防止旧 CAS 复活。

-- 与 ExecutionScope.hash 完全一致：按顺序拼接四字节大端 UTF-8 字节长度和 UTF-8 正文，
-- 最后计算 SHA-256。不能用字符数、分隔符拼接或 PostgreSQL 数组的文本表示替代。
-- STRICT 拒绝整个 NULL 数组；NULL 元素返回 NULL，不能被 string_agg 静默跳过后形成另一身份。
CREATE FUNCTION agent_runtime.scope_hash(p_parts text[])
RETURNS text
LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE SECURITY INVOKER
AS $$
  SELECT CASE WHEN array_position(p_parts, NULL) IS NOT NULL THEN NULL
    ELSE encode(sha256(COALESCE(string_agg(
      int4send(octet_length(convert_to(part, 'UTF8'))) || convert_to(part, 'UTF8'),
      ''::bytea ORDER BY position), ''::bytea)), 'hex') END
  FROM unnest(p_parts) WITH ORDINALITY AS parts(part, position)
$$;

COMMENT ON FUNCTION agent_runtime.scope_hash(text[]) IS
  'ExecutionScope.hash 的 UTF-8 长度前缀实现；NULL 元素不能构成有效 scope';

-- 所有已持久化 Attempt 都参与映射，不能只看 Command 当前的 attempt_id：人工恢复、重试和
-- 宕机前的旧检查点同样要擦除。来源使用 LEFT JOIN 且核对 owner，缺失或错归属的来源仍输出
-- NULL epoch/status，留给残留核验拒绝，不能在 INNER JOIN 中悄悄消失。
--
-- roots 在根/子两个分支复用，显式 NOT MATERIALIZED 允许调用方的 tenant/subject 条件下推；
-- 普通遗忘不需要在 Java 逐条读取 Attempt，也不需要物化全部用户的 scope。
CREATE FUNCTION agent_runtime.execution_scopes()
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
      r.namespace, r.user_id, r.session_id, i.call_id, true AS invocation_bound
    FROM roots r
    JOIN business.child_invocations i ON i.root_turn_id=r.root_turn_id
    UNION ALL
    -- 未绑定的 child:* 步骤不能恢复预算或授权，但其 call ID 足以定位旧 SDK 正文。
    -- substring 仅移除首个 child:；call ID 自己包含冒号时必须原样保留。
    SELECT DISTINCT r.tenant, r.subject, r.root_turn_id, r.release_hash, r.epoch, r.source_status,
      r.namespace, r.user_id, r.session_id, substring(s.namespace FROM 7), false
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
  FROM child_calls c
$$;

COMMENT ON FUNCTION agent_runtime.execution_scopes() IS
  '按全部根 Attempt 派生根/固定研究子 scope；未绑定 child 步骤仅用于擦除，不恢复执行权';

-- 只返回不安全行的归属和类别，不导出用户消息、工具结果、SDK payload 或 Artifact 正文。
-- 全局核验一次物化 scope，后续关联复用；仍有合法来源但属于旧 epoch 的正文同样是不安全。
-- 无法关联 scope 的非墓碑状态先按 user_id 归属到已知 owner；完全无主的状态输出 NULL/NULL，
-- 使快照的全局孤立计数无法漏掉它。合法无正文墓碑不要求仍有子调用记录。
CREATE FUNCTION agent_runtime.unsafe_execution_data()
RETURNS TABLE (tenant text, subject text, kind text)
LANGUAGE sql STABLE PARALLEL SAFE SECURITY INVOKER
AS $$
  WITH scopes AS MATERIALIZED (
    SELECT * FROM agent_runtime.execution_scopes()
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
  WHERE t.id IS NULL OR s.id IS NULL OR s.epoch<1 OR o.tenant IS NULL OR s.epoch<>o.epoch
    OR s.status='forgotten'
    OR (c.namespace LIKE 'child:%' AND NOT EXISTS (
      SELECT 1 FROM business.child_invocations i
      WHERE i.root_turn_id=c.turn_id AND i.call_id=substring(c.namespace FROM 7)))
  UNION ALL
  SELECT t.tenant, t.subject, 'child_invocations'::text
  FROM business.child_invocations c
  LEFT JOIN business.turns t ON t.id=c.root_turn_id
  LEFT JOIN memory.sources s ON s.id=t.id AND s.tenant=t.tenant AND s.subject=t.subject
  LEFT JOIN memory.owners o ON o.tenant=t.tenant AND o.subject=t.subject
  WHERE t.id IS NULL OR s.id IS NULL OR s.epoch<1 OR o.tenant IS NULL OR s.epoch<>o.epoch
    OR s.status='forgotten'
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
  -- owner 冲突时也标记 Artifact 自己声明的 owner，避免只检查其中一方时遗漏损坏正文。
  SELECT a.tenant, a.subject, 'tool_artifacts'::text
  FROM business.artifacts a
  JOIN business.turns t ON t.id=a.turn_id
  WHERE a.privacy_bound AND (a.tenant<>t.tenant OR a.subject<>t.subject)
$$;

COMMENT ON FUNCTION agent_runtime.unsafe_execution_data() IS
  '仅返回执行正文残留的 owner/kind；未知 owner 为 NULL，合法 SDK 墓碑不计残留';

-- 遗忘尾部按 owner 的 user hash 检查剩余 live scope；主键以 namespace 开头，不能支撑
-- 该查询的 owner 前缀。部分索引只包含有正文的状态，避免每次遗忘扫描其他用户及历史墓碑。
CREATE INDEX states_live_user ON agent_runtime.states(user_id) WHERE NOT deleted;

-- 工具文件既可能按根 Turn 的真实 owner 擦除，也可能按 Artifact 声明的 owner 清除损坏关联。
-- 两个候选入口都要有索引，不能从全体用户的 Artifact 元数据出发逐行计算跨 owner 的 OR。
-- 部分索引只覆盖运行时工具文件，普通业务 Artifact 不增加这两个索引的维护成本。
CREATE INDEX artifacts_private_turn ON business.artifacts(turn_id) WHERE privacy_bound;
CREATE INDEX artifacts_private_owner ON business.artifacts(tenant,subject) WHERE privacy_bound;

-- 调用方在同一事务持有 memory.owners 的用户锁，与根/子 SDK 写入的执行门禁互斥。
-- 删除 child_invocations 或 child:* 步骤前先定位并擦除所有旧 SDK 正文，否则会丢失子 scope。
-- 不能把状态行直接 DELETE：保留递增版本的墓碑，旧 expected-version 和新建 CAS 都不能复活。
-- 本函数不修改 owner epoch/屏障或遗忘回执；这些仍由现有遗忘编排及独立账本负责。
CREATE FUNCTION agent_runtime.erase_execution_data(
  p_tenant text, p_subject text, p_before_epoch bigint
)
RETURNS bigint
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY INVOKER
AS $$
DECLARE
  affected bigint := 0;
  changed bigint;
BEGIN
  IF p_tenant IS NULL OR length(btrim(p_tenant))=0
    OR p_subject IS NULL OR length(btrim(p_subject))=0
    OR p_before_epoch IS NULL OR p_before_epoch<2 THEN
    RAISE EXCEPTION 'execution_privacy_invalid_boundary';
  END IF;

  UPDATE agent_runtime.states s SET payload=NULL, deleted=true, version=s.version+1
  FROM agent_runtime.execution_scopes() x
  WHERE x.tenant=p_tenant AND x.subject=p_subject
    AND (x.epoch IS NULL OR x.epoch<1 OR x.source_status IS NULL
      OR x.source_status='forgotten' OR x.epoch<p_before_epoch)
    AND s.namespace=x.namespace AND s.user_id=x.user_id AND s.session_id=x.session_id
    AND NOT s.deleted;
  GET DIAGNOSTICS changed = ROW_COUNT;
  affected := affected + changed;

  DELETE FROM business.capability_steps c
  USING business.turns t
  LEFT JOIN memory.sources s ON s.id=t.id AND s.tenant=t.tenant AND s.subject=t.subject
  WHERE c.turn_id=t.id AND t.tenant=p_tenant AND t.subject=p_subject
    AND (s.id IS NULL OR s.epoch<1 OR s.status='forgotten' OR s.epoch<p_before_epoch);
  GET DIAGNOSTICS changed = ROW_COUNT;
  affected := affected + changed;

  -- 先按用户限定候选，再按 Artifact 主键删除。第一个入口使用 turn_owner_status 与
  -- artifacts_private_turn，第二个入口使用 artifacts_private_owner 与 Turn 主键；
  -- UNION 合并同一文件在两个入口的命中，避免遍历其他租户的文件元数据。
  WITH owner_turns AS NOT MATERIALIZED (
    SELECT t.id, t.tenant, t.subject, t.conversation_id, t.release_hash
    FROM business.turns t WHERE t.tenant=p_tenant AND t.subject=p_subject
  ), artifact_candidates AS (
    SELECT a.id
    FROM owner_turns t
    JOIN business.artifacts a ON a.turn_id=t.id AND a.privacy_bound
    LEFT JOIN memory.sources s ON s.id=t.id AND s.tenant=t.tenant AND s.subject=t.subject
    WHERE s.id IS NULL OR s.epoch<1 OR s.status='forgotten' OR s.epoch<p_before_epoch
      OR a.tenant<>t.tenant OR a.subject<>t.subject
      OR a.conversation_id<>t.conversation_id OR a.release_hash<>t.release_hash
    UNION
    -- 根身份与 Artifact 声明身份冲突时，该行已损坏；任一相关用户遗忘均擦除，
    -- 不能借另一个 owner 的来源 epoch 或失联根保留这份正文。仅访问声明属于本用户的行。
    SELECT a.id
    FROM business.artifacts a
    LEFT JOIN business.turns t ON t.id=a.turn_id
    WHERE a.tenant=p_tenant AND a.subject=p_subject AND a.privacy_bound
      AND (t.id IS NULL OR a.tenant<>t.tenant OR a.subject<>t.subject
        OR a.conversation_id<>t.conversation_id OR a.release_hash<>t.release_hash)
  )
  DELETE FROM business.artifacts a USING artifact_candidates c WHERE a.id=c.id;
  GET DIAGNOSTICS changed = ROW_COUNT;
  affected := affected + changed;

  DELETE FROM business.child_invocations c
  USING business.turns t
  LEFT JOIN memory.sources s ON s.id=t.id AND s.tenant=t.tenant AND s.subject=t.subject
  WHERE c.root_turn_id=t.id AND t.tenant=p_tenant AND t.subject=p_subject
    AND (s.id IS NULL OR s.epoch<1 OR s.status='forgotten' OR s.epoch<p_before_epoch);
  GET DIAGNOSTICS changed = ROW_COUNT;
  affected := affected + changed;

  -- 未知 live scope 不能仅因 epoch 已推进而被忽略。只扫描当前 owner 的有正文状态，
  -- 并要求它确实关联到有效的新来源及绑定调用；完整 scope 条件仍允许 planner 下推 owner。
  -- 账本 apply 在 owner epoch 提升前执行，所以同一事实重放时允许 before_epoch 本身；
  -- 更远的 future epoch 不可信，必须阻断事务，不能猜测归属或解除隐私屏障。
  IF EXISTS (
    SELECT 1 FROM agent_runtime.states s
    WHERE s.user_id=agent_runtime.scope_hash(ARRAY[p_tenant, p_subject]) AND NOT s.deleted
      AND NOT EXISTS (
        SELECT 1 FROM agent_runtime.execution_scopes() x
        JOIN memory.owners o ON o.tenant=x.tenant AND o.subject=x.subject
        WHERE x.tenant=p_tenant AND x.subject=p_subject
          AND x.namespace=s.namespace AND x.user_id=s.user_id AND x.session_id=s.session_id
          AND x.invocation_bound AND x.epoch>=p_before_epoch
          AND x.epoch<=GREATEST(o.epoch, p_before_epoch)
          AND x.source_status IS NOT NULL AND x.source_status<>'forgotten')) THEN
    RAISE EXCEPTION 'execution_privacy_residuals';
  END IF;
  RETURN affected;
END;
$$;

COMMENT ON FUNCTION agent_runtime.erase_execution_data(text,text,bigint) IS
  '持用户锁按来源 epoch 集合擦除执行正文；先 SDK 墓碑后删除步骤/调用，返回实际改变行数';
