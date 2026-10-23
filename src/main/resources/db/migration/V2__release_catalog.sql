-- 当前 Java 应用基线后的持久发布目录；不从任意历史 hash 伪造缺失的发布定义。
CREATE SCHEMA control_plane;

CREATE TABLE control_plane.release_manifests (
  release_hash text PRIMARY KEY CHECK(release_hash ~ '^[0-9a-f]{64}$'),
  manifest jsonb NOT NULL CHECK(jsonb_typeof(manifest)='object'),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE control_plane.deployments (
  name text PRIMARY KEY,
  release_hash text NOT NULL REFERENCES control_plane.release_manifests(release_hash),
  revision bigint NOT NULL CHECK(revision>0),
  updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE control_plane.deployment_changes (
  deployment text NOT NULL REFERENCES control_plane.deployments(name),
  revision bigint NOT NULL CHECK(revision>0),
  release_hash text NOT NULL REFERENCES control_plane.release_manifests(release_hash),
  previous_hash text REFERENCES control_plane.release_manifests(release_hash),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY(deployment,revision),
  CHECK((revision=1 AND previous_hash IS NULL) OR (revision>1 AND previous_hash IS NOT NULL))
);

-- 即使误授 UPDATE/DELETE，也不能原地改写发布定义和变更历史。
CREATE FUNCTION control_plane.reject_immutable_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'immutable release record' USING ERRCODE='55000';
END;
$$;
CREATE TRIGGER release_manifests_immutable BEFORE UPDATE OR DELETE
  ON control_plane.release_manifests FOR EACH ROW
  EXECUTE FUNCTION control_plane.reject_immutable_mutation();
CREATE TRIGGER deployment_changes_immutable BEFORE UPDATE OR DELETE
  ON control_plane.deployment_changes FOR EACH ROW
  EXECUTE FUNCTION control_plane.reject_immutable_mutation();
