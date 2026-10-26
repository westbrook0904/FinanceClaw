-- 授权事实只保存资源身份、精确发布、接收主体与单调修订，不保存任何凭据值。
ALTER TABLE control_plane.agents ADD COLUMN archived boolean NOT NULL DEFAULT false;

CREATE TABLE control_plane.resource_grants (
  resource_kind text NOT NULL CHECK(resource_kind IN ('agent','credential')),
  resource_id text NOT NULL CHECK(length(resource_id) BETWEEN 1 AND 200),
  resource_tenant text NOT NULL CHECK(length(resource_tenant) BETWEEN 1 AND 200),
  resource_subject text NOT NULL CHECK(length(resource_subject) BETWEEN 1 AND 200),
  grantee_tenant text NOT NULL CHECK(length(grantee_tenant) BETWEEN 1 AND 200),
  grantee_subject text NOT NULL CHECK(length(grantee_subject) BETWEEN 1 AND 200),
  release_hash text,
  binding_hash text,
  revision bigint NOT NULL CHECK(revision > 0),
  enabled boolean NOT NULL,
  changed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  CHECK ((resource_kind='agent' AND release_hash IS NOT NULL AND release_hash ~ '^[0-9a-f]{64}$' AND binding_hash IS NULL)
      OR (resource_kind='credential' AND release_hash IS NULL AND binding_hash IS NOT NULL
          AND binding_hash ~ '^[0-9a-f]{64}$')),
  PRIMARY KEY(resource_kind,resource_id,grantee_tenant,grantee_subject)
);
CREATE INDEX resource_grants_grantee ON control_plane.resource_grants
  (grantee_tenant,grantee_subject,resource_kind,resource_id) WHERE enabled;

CREATE TABLE control_plane.resource_grant_audit (
  resource_kind text NOT NULL,
  resource_id text NOT NULL,
  resource_tenant text NOT NULL,
  resource_subject text NOT NULL,
  grantee_tenant text NOT NULL,
  grantee_subject text NOT NULL,
  release_hash text,
  binding_hash text,
  revision bigint NOT NULL,
  enabled boolean NOT NULL,
  changed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY(resource_kind,resource_id,grantee_tenant,grantee_subject,revision)
);
CREATE TRIGGER resource_grant_audit_immutable BEFORE UPDATE OR DELETE
  ON control_plane.resource_grant_audit FOR EACH ROW
  EXECUTE FUNCTION control_plane.reject_immutable_mutation();

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'financeclaw_app') THEN
    GRANT SELECT, INSERT, UPDATE ON control_plane.resource_grants TO financeclaw_app;
    GRANT SELECT, INSERT ON control_plane.resource_grant_audit TO financeclaw_app;
  END IF;
END;
$$;
