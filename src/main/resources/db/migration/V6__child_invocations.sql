-- 固定市场研究子调用的持久身份和累计预算。
-- 子调用沿用根 Turn 的 Command、租约、取消、授权和隐私边界，不创建子队列或第二套状态机。
-- owner/conversation/release 从 business.turns 派生，epoch 从同 owner 的 memory.sources 派生，
-- grant revision 从 business.turn_policies 派生；原始输入与结果正文不在此表复制。
-- 最终结果继续使用 capability_steps 中 (root_turn_id, 'child:' || call_id, 'result') 的原子回执。
CREATE TABLE business.child_invocations (
  root_turn_id text NOT NULL REFERENCES business.turns(id) ON DELETE CASCADE,
  call_id text NOT NULL CHECK(length(btrim(call_id)) > 0),
  target text NOT NULL DEFAULT 'market_research' CHECK(target = 'market_research'),
  input_hash text NOT NULL CHECK(input_hash ~ '^[0-9a-f]{64}$'),
  scopes jsonb NOT NULL DEFAULT '["market:read"]'::jsonb
    CHECK(scopes = '["market:read"]'::jsonb),
  status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'completed')),
  model_calls integer NOT NULL DEFAULT 0 CHECK(model_calls BETWEEN 0 AND 4),
  tool_calls integer NOT NULL DEFAULT 0 CHECK(tool_calls BETWEEN 0 AND 10),
  PRIMARY KEY(root_turn_id, call_id)
);
COMMENT ON TABLE business.child_invocations IS
  '根 Turn 内固定市场研究调用身份及累计预算；结果只引用现有 capability_steps，不独立调度';

-- 新表须对已有应用角色显式授权。新库尚未创建该角色时，由 init-db 现有 grants.sql 统一授权。
-- 不向受限 memory-worker 授予 business schema 或本表权限。
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'financeclaw_app') THEN
    GRANT SELECT, INSERT, UPDATE, DELETE ON business.child_invocations TO financeclaw_app;
  END IF;
END;
$$;
