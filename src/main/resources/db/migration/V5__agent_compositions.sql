-- 旧草稿保持 NULL，表示 P2a 继承语义；不重写任何旧清单或摘要。
ALTER TABLE control_plane.agent_draft_revisions ADD COLUMN composition jsonb
  CHECK(composition IS NULL OR jsonb_typeof(composition)='object');

ALTER TABLE control_plane.release_manifests
  DROP CONSTRAINT release_manifests_manifest_format_check;
ALTER TABLE control_plane.release_manifests
  ADD CONSTRAINT release_manifests_manifest_format_check
  CHECK(manifest_format IN ('legacy-v1','agent-v2','agent-v3'));
