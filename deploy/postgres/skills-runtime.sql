-- 已有开发库的 Skills 字段补齐；空库仍使用 0001_initial。
-- 在 financeclaw_app 执行。历史数据早于 Skills 发布，新增来源字段回填空数组。
-- 保留原有行、外键与唯一约束；字段已存在时不覆盖数据。失败整笔回滚。
BEGIN;
SET LOCAL lock_timeout = '3s';
SET LOCAL statement_timeout = '15s';

ALTER TABLE conversation_messages
    ADD COLUMN IF NOT EXISTS skill_access_refs JSON NOT NULL DEFAULT '[]'::json;
ALTER TABLE model_context_manifests
    ADD COLUMN IF NOT EXISTS skill_catalog_hash VARCHAR(64),
    ADD COLUMN IF NOT EXISTS skill_catalog_omitted INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS skill_refs JSON NOT NULL DEFAULT '[]'::json,
    ADD COLUMN IF NOT EXISTS skill_resource_refs JSON NOT NULL DEFAULT '[]'::json,
    ADD COLUMN IF NOT EXISTS skill_access_refs JSON NOT NULL DEFAULT '[]'::json;
ALTER TABLE notification_targets ALTER COLUMN turn_id DROP NOT NULL;

-- 回填后移除临时数据库默认值，与当前初始迁移及 ORM 的结构保持一致。
ALTER TABLE conversation_messages ALTER COLUMN skill_access_refs DROP DEFAULT;
ALTER TABLE model_context_manifests
    ALTER COLUMN skill_catalog_omitted DROP DEFAULT,
    ALTER COLUMN skill_refs DROP DEFAULT,
    ALTER COLUMN skill_resource_refs DROP DEFAULT,
    ALTER COLUMN skill_access_refs DROP DEFAULT;
COMMIT;
