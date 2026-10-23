-- 为新格式添加独立解码标记；已有 legacy 正文与 hash 完全保留。
ALTER TABLE control_plane.release_manifests
  ADD COLUMN manifest_format text NOT NULL DEFAULT 'legacy-v1'
  CHECK(manifest_format IN ('legacy-v1','agent-v2'));

CREATE TABLE control_plane.agents (
  id text PRIMARY KEY CHECK(id ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'),
  tenant text NOT NULL CHECK(length(btrim(tenant))>0),
  subject text NOT NULL CHECK(length(btrim(subject))>0),
  draft_revision bigint NOT NULL CHECK(draft_revision>0),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX agents_owner ON control_plane.agents(tenant,subject,created_at,id);

CREATE TABLE control_plane.agent_draft_revisions (
  agent_id text NOT NULL REFERENCES control_plane.agents(id),
  revision bigint NOT NULL CHECK(revision>0),
  display_name text NOT NULL CHECK(length(display_name) BETWEEN 1 AND 200),
  system_prompt text NOT NULL CHECK(octet_length(system_prompt) BETWEEN 1 AND 16384),
  base_release_hash text NOT NULL REFERENCES control_plane.release_manifests(release_hash),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY(agent_id,revision)
);

CREATE FUNCTION control_plane.guard_agent_identity() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.id IS DISTINCT FROM OLD.id OR NEW.tenant IS DISTINCT FROM OLD.tenant
     OR NEW.subject IS DISTINCT FROM OLD.subject THEN
    RAISE EXCEPTION 'agent owner and identity are immutable' USING ERRCODE='23514';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER agent_identity_immutable BEFORE UPDATE ON control_plane.agents
  FOR EACH ROW EXECUTE FUNCTION control_plane.guard_agent_identity();
CREATE TRIGGER agent_drafts_immutable BEFORE UPDATE OR DELETE ON control_plane.agent_draft_revisions
  FOR EACH ROW EXECUTE FUNCTION control_plane.reject_immutable_mutation();

-- 历史任务沿用原 SDK scope 的 Agent 名称，不重算发布或检查点键。
ALTER TABLE business.conversations ADD COLUMN agent_id text NOT NULL DEFAULT 'finance_agent';
ALTER TABLE business.turns ADD COLUMN agent_id text NOT NULL DEFAULT 'finance_agent';

CREATE OR REPLACE FUNCTION business.guard_conversation_release() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.release_hash IS NOT NULL AND (
      NEW.release_hash IS DISTINCT FROM OLD.release_hash OR NEW.agent_id IS DISTINCT FROM OLD.agent_id) THEN
    RAISE EXCEPTION 'conversation release is immutable' USING ERRCODE='23514';
  END IF;
  RETURN NEW;
END;
$$;
DROP TRIGGER conversation_release_immutable ON business.conversations;
CREATE TRIGGER conversation_release_immutable BEFORE UPDATE OF release_hash,agent_id
  ON business.conversations FOR EACH ROW EXECUTE FUNCTION business.guard_conversation_release();

CREATE FUNCTION business.guard_turn_target() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.agent_id IS DISTINCT FROM OLD.agent_id OR NEW.release_hash IS DISTINCT FROM OLD.release_hash THEN
    RAISE EXCEPTION 'turn target is immutable' USING ERRCODE='23514';
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER turn_target_immutable BEFORE UPDATE OF agent_id,release_hash ON business.turns
  FOR EACH ROW EXECUTE FUNCTION business.guard_turn_target();
