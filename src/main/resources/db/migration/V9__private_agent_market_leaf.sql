-- 私有同步委派新增一个严格收窄的行情执行档位；不新增表、队列、子租约或恢复权威。
-- 旧空工具调用继续保存 scopes=[]、tool_calls=0，固定研究调用的约束也逐字保留。
-- 新档位只允许一次实际 market_snapshot 读取；重复步骤回放复用 capability_steps，
-- 不再次消耗子/根工具额度。身份仍由 V8 的 immutable trigger 保护，不能原地升级旧调用。
ALTER TABLE business.child_invocations
  DROP CONSTRAINT child_invocations_target_binding_check;

ALTER TABLE business.child_invocations
  ADD CONSTRAINT child_invocations_target_binding_check CHECK (
    (target='market_research' AND target_agent_id IS NULL AND target_release_hash IS NULL
      AND scopes='["market:read"]'::jsonb)
    OR (target='private_agent' AND target_agent_id IS NOT NULL AND target_release_hash IS NOT NULL
      AND ((scopes='[]'::jsonb AND tool_calls=0)
        OR (scopes='["market:read"]'::jsonb AND tool_calls BETWEEN 0 AND 1))));

-- 保持 V8 的集合接口。执行 scope 只依赖原有冻结身份，因此 execution_scopes()、
-- unsafe_execution_data() 与 erase_execution_data() 都可继续复用，不复制另一套清理查询。
-- 旧 empty 与新 market 两档按目标冻结计划互斥：同一 agent/release 不会在两档间歧义映射。
-- 这里仅核验恢复时的隐私元数据，不验证父契约授权、不解析 current deployment，也不授予执行资格。
-- Java 仍须验证完整清单摘要、运行工件、父 agent-leaf/2 契约及实时权限交集。
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
        AND a.id IS NOT NULL AND a.tenant=t.tenant AND a.subject=t.subject
        AND m.manifest_format='agent-v3' AND m.manifest->>'format'='agent-release/3'
        AND m.manifest->>'agentId'=i.target_agent_id
        AND m.manifest->>'tenant'=t.tenant AND m.manifest->>'subject'=t.subject
        AND (
          -- agent-leaf/1 原有空工具身份不变，不能因新档位放宽而借到行情权限。
          (i.scopes='[]'::jsonb AND i.tool_calls=0
            AND m.manifest#>'{plan,requestedCapabilities}'='[]'::jsonb
            AND m.manifest#>'{plan,capabilities}'='[]'::jsonb
            AND m.manifest#>'{plan,definition,scopes}'='[]'::jsonb)
          OR
          -- 发布 resolver 按能力 ID 排序：snapshot 在前，隐含 reader 在后。
          -- reader 自身 scopes=[]，不能错误要求目标额外拥有 artifact:read。
          -- 新委派契约只执行 snapshot；reader 是既有发布闭包，不能被子运行时装配。
          -- 数组长度与每项权限/依赖都精确匹配，额外工具、重复项和权限扩张均不算合法绑定。
          (i.scopes='["market:read"]'::jsonb AND i.tool_calls BETWEEN 0 AND 1
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
        ), false)
      ELSE false
    END
  FROM business.child_invocations i
  LEFT JOIN business.turns t ON t.id=i.root_turn_id
  LEFT JOIN control_plane.agents a ON a.id=i.target_agent_id
  LEFT JOIN control_plane.release_manifests m ON m.release_hash=i.target_release_hash
$$;

COMMENT ON FUNCTION agent_runtime.child_invocation_bindings() IS
  '集合核对研究、空工具或单次行情私有叶子；失效目标仍保留原始定位字段供遗忘擦除';
